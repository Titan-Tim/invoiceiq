# Invoice-IQ — Practice / Bureau Edition

**Build brief v1.0 · 2026-08-25**
Source of truth for turning Invoice-IQ from single-business into a multi-client bureau product.

---

## 1. Why this exists

A local admin-support / bookkeeping bureau does the accounts for many different businesses. Critically, **she uses a different accounting package per client** — Sage 50 for some, Xero for others, QuickBooks Online for others. She is very interested in Invoice-IQ's AP automation, but she is not a *customer running it for one business* — she is an **agent running it on behalf of many businesses**.

Invoice-IQ today assumes **one deployment = one business = one accounting package**. So as it stands she would need a **separate instance and a separate login per client**, each with its own hosting and its own finance connection. For a bureau with a handful (or dozens) of small clients that is unworkable: too many logins, too many deployments, too much cost.

This brief specifies a **Practice / Bureau edition**: one instance, one login for the bureau, a **client switcher**, and **per-client** finance configuration and data isolation — mirroring the mental model she already knows from *My Xero* and *QuickBooks Online Accountant*.

### Market note
Bookkeeping bureaux and outsourced-finance practices are an underserved, high-fit market for AP automation: they feel the pain across many clients at once, they are repeat buyers, and they are natural white-label/reseller channels. This edition is a productisable tier — **"Invoice-IQ for Bookkeepers / Practices"** — that fits the existing white-label/SaaS direction.

---

## 2. The key insight: the three finance systems are NOT the hard part

Invoice-IQ already supports all three packages. The connector factory (`src/connectors/factory.py`) picks a connector — `SageConnector`, `QBOConnector`, `XeroConnector` (or `LedgerIQConnector`) — from a single `finance_system` setting, and each connector reads its own credential block and takes a `settings` dict as a constructor argument. A single instance can already talk to any of the three.

**The blockers to one shared bureau instance are tenancy, not connectivity:**

1. **One global `finance_system`.** There is exactly one value app-wide. A bureau needs Client A = Xero, Client B = Sage, Client C = QBO — all live simultaneously.
2. **Un-scoped data.** Invoices, POs, suppliers, approvals, remittances and the audit log sit in one flat pool with no "which client" tag. For a bureau this is both a usability problem and a **data-separation / compliance** problem — each client's financial data must stay cleanly isolated (also relevant to the SOC 2 / ISO 27001 ambitions).
3. **One OAuth connection per type.** Client A's Xero and Client D's Xero would collide — tokens are stored per *type*, not per *client*.
4. **Single-business plumbing** for email ingestion, users, and audit.

The good news: two recent foundations move us toward this rather than away from it.

---

## 3. Foundations we already have (reuse, don't rebuild)

- **Three-connector factory** taking a `settings` dict — we can build a *per-client* connector by handing it that client's settings block. No connector rewrite needed.
- **DB-backed keyed config/token storage** (`src/config_manager.py`, added 2026-08-25). Settings and OAuth tokens now live in the Postgres `app_config` (key, value) table via `load_settings`/`save_settings` and `load_tokens`/`save_tokens`/`delete_tokens(name)`. This is **keyed storage** — per-client keys (`settings:<client_id>`, `tokens_xero:<client_id>`) slot straight in.
- **Four-tier RBAC** (superadmin > admin > approver > standard) with live-DB role reads — a sensible base to extend to bureau roles.
- **Postgres + single web worker** on Render — no cross-worker cache-coherence problems to design around.

---

## 4. Core concept

Introduce a **Client** (tenant) entity. Every piece of financial data belongs to exactly one Client. The bureau user picks the **active client** from a switcher; everything they see and do is scoped to that client. Each Client carries **its own finance system + credentials + OAuth tokens + email intake**.

```
Bureau (the practice)
 ├── Bureau users (staff logins: owner / manager / operator)
 └── Clients
      ├── Client A  → Xero      (own tokens, own suppliers, own invoices, own inbox)
      ├── Client B  → Sage 50   (own config,  own suppliers, own invoices, own inbox)
      └── Client C  → QuickBooks (own tokens, own suppliers, own invoices, own inbox)
```

---

## 5. Data model changes

### New tables
- **`clients`** — `id`, `name`, `slug`, `finance_system` ('sage'|'qbo'|'xero'|'ledgeriq'), `is_active`, `created_at`, plus per-client display bits (currency, company name). Per-client *credentials and OAuth tokens* live in `app_config` under client-scoped keys (keeps secrets out of the ORM and reuses the store built on 2026-08-25).
- **`client_users`** — bridge table (`client_id`, `user_id`, optional per-client role) so a staff member can be granted access to some or all clients. (A bureau owner sees all; an operator might be limited to assigned clients.)

### Add `client_id` (FK, indexed, NOT NULL) to the top-level records
Based on the current schema in `src/database.py`:
- **`invoices`** — add `client_id`.
- **`purchase_orders`** — add `client_id`. ⚠️ **`po_number` is currently globally `unique=True`** — this must become **unique per client** (`UniqueConstraint('client_id','po_number')`), otherwise two clients can't both have "PO-1001".
- **`remittances`** — add `client_id`.
- Child tables **`invoice_lines`** and **`po_lines`** inherit scope through their parent (no column needed, but every query must join/filter via the parent).
- **`audit_log`** — add `client_id` (it currently links only via `invoice_id`; scoping it directly makes per-client audit export clean and keeps non-invoice events attributable).

### Users
- `users` stays **global** (one login per staff member; `email` stays globally unique). Client access is granted via `client_users`. A "bureau owner" role sits above the existing superadmin.

### Query scoping (the bulk of the work)
Every query that reads or writes invoices/POs/suppliers/remittances/audit must filter by the **active client**. Approaches, cheapest-risk first:
- A single `current_client_id()` helper (from session) + a disciplined pass over every query in `app.py`, `invoice_processor.py`, `po_matcher.py`, `remittance_processor.py`, `approval.py`.
- Optionally a SQLAlchemy default-scope / query helper to make "unscoped query" the exception, reducing the chance of a leak.
- **Every list/detail/action endpoint must reject records whose `client_id` ≠ active client** (defence against ID tampering — a bureau handling multiple clients' finances cannot afford a cross-client leak).

---

## 6. Finance connections — per client

- The wizard/settings "Finance System" step becomes **per client**: pick this client's package, enter its credentials, connect its OAuth.
- `get_connector()` gains a client argument: it loads *that client's* settings block (`load_settings(client_id)`) and constructs the right connector. The connector classes are unchanged.
- OAuth routes (`/auth/qbo/*`, `/auth/xero/*`) carry the client context (e.g. in `session` alongside `oauth_state`) so the callback stores tokens under `tokens_xero:<client_id>`. Builds directly on the per-route-connector fix from 2026-08-25.
- Background token refresh already runs per-connector; it just needs the client key.

---

## 7. Auth & roles (bureau model)

Extend the existing hierarchy:

| Role | Scope |
|---|---|
| **Bureau owner** | Everything: manage clients, connect finance systems, manage staff, all client data |
| **Bureau manager** | All clients' data + user mgmt within the practice; cannot change billing/licence |
| **Operator** | Only clients they're assigned to (via `client_users`); process/approve within those |
| *(future)* **Client viewer** | A client's own staff, read-only access to *their* client only |

The existing superadmin/admin/approver/standard tiers map inside a single client's context; the new bureau tiers sit above and control cross-client visibility.

---

## 8. Client switcher UX

- Persistent **client selector** in the top bar / sidebar header (name + finance-system badge, searchable for many clients).
- Selecting a client sets `session['client_id']`; all pages re-render scoped to it. The existing sidebar "system badge" becomes the *active client's* system.
- **Clients** management page: list, add, edit, archive; per-client "Connect finance system" + health/last-sync indicator; per-client email intake address.
- A **bureau dashboard**: cross-client roll-up (e.g. invoices awaiting approval across all clients, failed pushes per client) so she can triage her whole book from one screen — a genuine selling point over per-instance tools.

---

## 9. Email ingestion per client

Today ingestion is one mailbox/poll for one business. Options for per-client routing (pick per deployment):
- **Plus-addressing / per-client alias** — `apinbox+clientA@…` routed by the suffix.
- **Per-client forwarding address** — each client forwards supplier invoices to a unique address that maps to their `client_id`.
- **Per-client mailbox** — heavier; only if a client insists on a dedicated inbox.
Whichever is chosen, the intake pipeline tags the resulting invoice with the correct `client_id` **before** extraction/matching.

---

## 10. Migration plan (existing single-tenant installs → bureau)

The change must not break existing single-business deployments (Ledger-IQ demo, any live pilots).
1. Create a **default Client** ("Primary") from the current instance's existing settings.
2. Backfill `client_id` on all existing invoices/POs/remittances/audit rows to that default client.
3. Move the current single `settings` block to `settings:<default_client_id>`; move `tokens_qbo`/`tokens_xero` to the client-scoped keys.
4. Add a `bureau_mode` flag: **off** = behave exactly as today (switcher hidden, one implicit client); **on** = show switcher + client management. A normal single-business customer never sees the bureau UI.

This keeps one codebase serving both the single-business tier and the practice tier.

---

## 11. Isolation, security & compliance

- **Hard scoping** on every query + endpoint-level ownership checks (no client sees another's data).
- Per-client **audit export** (supports each client's own record-keeping and any accountant handover).
- Secrets remain in `app_config` (never in the ORM/records), now client-scoped.
- Documented data-separation model strengthens the SOC 2 / ISO 27001 story — a bureau will *ask* how their clients' data is separated.
- Decision point: **shared-DB row-scoped** (simplest, one instance) vs **DB-per-client** (strongest isolation, heavier ops). Recommended: shared-DB row-scoped for the bureau tier, with the existing instance-per-customer model still available for clients who demand physical separation.

---

## 12. Licensing & commercial model

- Fits the existing per-instance licence/kill-switch (`src/licence.py` / `soliq_licence.py`) — a **bureau licence** with a **client count** entitlement (e.g. tiers: up to 5 / 15 / unlimited clients).
- Natural pricing: a practice base fee + per-active-client, undercutting the cost of standing up an instance each.
- Bureau is also a **reseller channel** — she could onboard her own clients under her practice, feeding the white-label goal.

---

## 13. Phased delivery

**Phase 0 — Spec sign-off (this doc).** Confirm scope + the open questions in §14.

**Phase 1 — Tenancy core.** `clients` + `client_users` tables; `client_id` on invoices/POs/remittances/audit; per-client PO-number uniqueness; migration to a default client; `bureau_mode` flag (off by default). No UI yet — proves data model + backfill on a branch without changing existing behaviour.

**Phase 2 — Per-client finance config.** Client-scoped `load_settings`/tokens; `get_connector(client_id)`; OAuth routes carry client context. One instance connecting Client A=Xero + Client B=QBO simultaneously.

**Phase 3 — Bureau UI.** Client switcher, Clients management page, per-client connect + health, scoped rendering across all pages, bureau roles.

**Phase 4 — Bureau dashboard + email routing.** Cross-client roll-up; per-client intake addresses.

**Phase 5 — Licensing + polish.** Client-count entitlement; onboarding flow for adding a client; demo data across mixed finance systems for sales.

A working **demo for her** is reachable at end of Phase 3 (she could switch between a Xero client and a Sage client live). Phases 1–2 are the real engineering; 3+ build on them.

---

## 14. Open questions for Tim

1. **Data isolation stance:** shared-DB row-scoped bureau instance (recommended) or hold the line on instance-per-client for isolation? (Affects everything downstream.)
2. **Client count:** roughly how many clients does she have, and are they mostly small? (Shapes switcher UX + pricing tiers.)
3. **Email intake:** are her clients happy to forward invoices to a per-client address, or do they expect a dedicated mailbox each?
4. **Client self-access:** does she want to (eventually) give each client read-only sight of their own AP, or is this purely her internal tool?
5. **Pricing:** practice base + per-client, or flat unlimited-clients bureau fee?
6. **Pilot first?** Validate willingness-to-pay with 1–2 instance-per-client setups before committing to Phases 1–3, or go straight to the build on the strength of her interest?

---

## 15. Non-goals (for this edition)

- Not building a full practice-management / GL product — Invoice-IQ stays **AP-automation-first** (plus the existing AR/deliver-to-invoice feature), it just becomes multi-client.
- Not replacing Sage/Xero/QBO — it feeds them, per client.
- Not multi-*bureau* SaaS in Phase 1 (one practice per instance first; a platform of many bureaux is a later step that reuses this tenancy layer).

---

*Prepared from the live codebase (`D:\Claude Projects\AP Automation`): Flask app, `src/connectors/factory.py` + Sage/QBO/Xero/Ledger-IQ connectors, `src/database.py` models (Invoice, InvoiceLine, PurchaseOrder, POLine, AuditLog, Remittance, User), DB-backed `config_manager` (app_config store), four-tier RBAC.*
