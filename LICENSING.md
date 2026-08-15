# Invoice-IQ on-prem licensing

Invoice-IQ's **on-prem / Solo** edition is gated by a signed Sol-IQ licence
(`@sol-iq/licence`). The **cloud/Render SaaS** deployment is unaffected — the gate is a
complete no-op unless explicitly enabled, and the verifier (`cryptography`) is imported
lazily, so cloud never loads it.

## Enable on-prem enforcement
Set these env vars in the Solo / on-prem deployment only:

| Var | Purpose | Default |
|---|---|---|
| `INVOICEIQ_LICENCE_ENFORCED` | Turn the gate on (`1`/`true`) | unset → **off** (cloud) |
| `INVOICEIQ_LICENCE_PATH` | Path to the signed licence file | `config/licence.soliq` |
| `INVOICEIQ_INSTALL_ID` | (optional) machine id for install-bound licences | unset |

## Behaviour (fail-soft)
- **Reads are never blocked.** Viewing invoices, dashboards, settings always works.
- Only mutating "processing" actions pause when a licence lapses/invalid — anything
  `POST/PUT/DELETE` under `/api/invoices`, `/api/remittances`, `/api/email/poll`,
  `/api/finance/sync-pos` → HTTP `402` (API) or a flash + redirect (pages).
- Grace period + renewal reminders are handled by the verifier; templates can show a
  banner via the `licence_status` context variable (`.message`, `.severity`).
- Module entitlements available via `licence_has_module('capture'|'approval'|'export')`.

## Issue a licence
From the suite programme (`suite-onprem-licensing`):
```
node tools/licence-issuer/issue.js --product INVOICEIQ \
  --customer "Firm LLP" --installId ANY --seats 10 --months 12 \
  --modules capture,approval,export --out licences/firm.soliq
```
Use `--installId INSTALL-XXXXXXXX` (the id shown at install) to bind the licence to one
machine; `ANY` = floating.

## Code
- `src/soliq_licence.py` — vendored shared verifier (do not edit; re-vendor from the suite).
- `src/licence.py` — Invoice-IQ wrapper (enforcement flag, cached status, action gating).
- `app.py` — `enforce_licence()` before_request + `inject_licence()` context processor.
