import json
import os
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path

import requests
from werkzeug.utils import secure_filename

from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, send_file, flash)

from src.database import db, Invoice, InvoiceLine, PurchaseOrder, POLine, User, AuditLog, Remittance
from src.config_manager import load_settings, save_settings
from src.approval import ApprovalWorkflow
from src.connectors.factory import get_connector, get_system_name


class _PrefixMiddleware:
    """Serve the app correctly when a reverse proxy mounts it under a sub-path
    (e.g. jasmitan.co.uk/invoice/…). The proxy forwards the full "/invoice/…"
    path; we move the prefix into SCRIPT_NAME so url_for() and redirects emit
    "/invoice/…" links, while routes still match on the bare path. A request
    without the prefix (Render health checks, direct *.onrender.com access)
    passes through unchanged, so the app stays usable at its root too."""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.prefix or path.startswith(self.prefix + '/'):
            environ['SCRIPT_NAME'] = self.prefix + environ.get('SCRIPT_NAME', '')
            environ['PATH_INFO'] = path[len(self.prefix):] or '/'
        return self.wsgi_app(environ, start_response)


def _run_migrations():
    """Lightweight in-place schema upgrades for installs created before a
    column existed — avoids requiring a separate migration tool for what is
    currently a single additive change (users.password_hash)."""
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    if 'users' not in inspector.get_table_names():
        return
    existing_cols = {c['name'] for c in inspector.get_columns('users')}
    if 'password_hash' not in existing_cols:
        db.session.execute(text('ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)'))
        db.session.commit()


def create_app():
    app = Flask(__name__)

    # When deployed behind the Jasmitan portal proxy, URL_PREFIX is set to
    # "/invoice" so the app serves under jasmitan.co.uk/invoice/…. Left unset
    # for standalone / direct deployments.
    url_prefix = os.environ.get('URL_PREFIX', '').rstrip('/')
    if url_prefix:
        app.wsgi_app = _PrefixMiddleware(app.wsgi_app, url_prefix)

    app.secret_key = os.environ.get('SECRET_KEY', 'ap-auto-change-in-prod-2024')
    if app.secret_key == 'ap-auto-change-in-prod-2024' and os.environ.get('DATABASE_URL'):
        app.logger.warning(
            'SECRET_KEY env var is not set — using the insecure default. '
            'Set SECRET_KEY in your hosting environment before going live.'
        )

    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        # Render/Heroku-style URLs use the legacy "postgres://" scheme;
        # SQLAlchemy's psycopg2 driver requires "postgresql://".
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    else:
        base_dir = Path(__file__).parent
        db_path  = base_dir / 'data' / 'invoiceiq.db'
        db_path.parent.mkdir(exist_ok=True)
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    db.init_app(app)
    with app.app_context():
        db.create_all()
        _run_migrations()

    # ------------------------------------------------------------------ #
    # Template context — system name available in every template
    # ------------------------------------------------------------------ #

    @app.context_processor
    def inject_globals():
        settings = load_settings()
        currency_symbols = {'GBP': '£', 'USD': '$', 'EUR': '€'}
        return {
            'finance_system_name': get_system_name(settings),
            'finance_system_key':  settings.get('finance_system', 'sage'),
            'currency_symbol':     currency_symbols.get(settings.get('app', {}).get('currency', 'GBP'), '£'),
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _user_name():  return session.get('user_name', 'System')
    def _user_id():    return session.get('user_id')

    PUBLIC_ENDPOINTS = {'login', 'forgot_password', 'healthz', 'static', 'privacy_policy', 'terms_of_use'}
    WIZARD_PATH_PREFIXES = ('/api/wizard', '/api/settings', '/auth/')

    @app.before_request
    def require_login():
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return
        # The entire first-run wizard (its pages and the API calls it makes —
        # saving settings, testing connections, OAuth) runs before any account
        # exists, so it's exempt until setup_complete flips to True.
        if not load_settings().get('app', {}).get('setup_complete'):
            if request.endpoint in ('wizard', 'dashboard') or request.path.startswith(WIZARD_PATH_PREFIXES):
                return
        if session.get('user_id') or session.get('user_name'):
            user_id = session.get('user_id')
            if (user_id and not request.path.startswith('/api/')
                    and request.endpoint not in ('settings_page', 'logout')):
                user = db.session.get(User, user_id)
                if user and user.must_change_password:
                    flash('Please set a new password to continue.', 'warning')
                    return redirect(url_for('settings_page'))
            return
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Authentication required'}), 401
        return redirect(url_for('login'))

    # ------------------------------------------------------------------ #
    # Page routes
    # ------------------------------------------------------------------ #

    @app.route('/healthz')
    def healthz():
        """Health-check endpoint — always returns 200 (used by preview/load-balancers)."""
        return '', 200

    @app.route('/', methods=['GET', 'HEAD'])
    def dashboard():
        # Health-check probes use HEAD — return 200 without a redirect
        from flask import make_response
        if request.method == 'HEAD':
            return make_response('', 200)
        settings = load_settings()
        if not settings.get('app', {}).get('setup_complete'):
            return redirect(url_for('wizard'))
        if not session.get('user_name'):
            return redirect(url_for('login'))
        return render_template('dashboard.html',
                               user_name=session.get('user_name'),
                               user_role=session.get('user_role', 'admin'))

    @app.route('/wizard')
    def wizard():
        """First-run setup wizard. Once setup is complete it remains available
        to admins as a 'reconfigure' tool for refreshing connections — e.g.
        re-authorising QuickBooks / Xero, or updating Sage / email / AI keys."""
        settings = load_settings()
        reconfigure = False
        if settings.get('app', {}).get('setup_complete'):
            # First run is over: the wizard is now an admin-only settings tool,
            # not a login gate. Existing customers go straight to the dashboard.
            if not session.get('user_id'):
                return redirect(url_for('login'))
            if session.get('user_role') != 'admin':
                flash('Only admins can change connection settings.', 'warning')
                return redirect(url_for('dashboard'))
            reconfigure = True
        step = request.args.get('step', '1')
        auth_result = {
            'success': request.args.get('auth_success', ''),
            'error':   request.args.get('auth_error',   ''),
        }
        return render_template('wizard.html', current_step=step,
                               auth_result=auth_result, reconfigure=reconfigure)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            email    = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            user = User.query.filter_by(email=email, is_active=True).first()
            if user and user.check_password(password):
                session.update({'user_id': user.id, 'user_name': user.name,
                                'user_role': user.role})
                if user.must_change_password:
                    flash('Please set a new password to continue.', 'warning')
                    return redirect(url_for('settings_page'))
                return redirect(url_for('dashboard'))
            error = 'Incorrect email or password.'
        return render_template('login.html', error=error)

    @app.route('/forgot-password', methods=['GET', 'POST'])
    def forgot_password():
        submitted = False
        if request.method == 'POST':
            email = request.form.get('email', '').strip().lower()
            user = User.query.filter_by(email=email, is_active=True).first()
            if user:
                temp_password = secrets.token_urlsafe(9)
                user.set_password(temp_password)
                user.must_change_password = True
                db.session.commit()
                try:
                    _send_password_reset_email(user, temp_password)
                except Exception as e:
                    app.logger.error(f"Forgot-password email failed: {e}")
            submitted = True
        return render_template('forgot_password.html', submitted=submitted)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    def _legal_doc_context():
        settings = load_settings()
        admin = User.query.filter_by(role='admin').order_by(User.id).first()
        return {
            'today':         datetime.utcnow().strftime('%d %B %Y'),
            'company_name':  settings.get('app', {}).get('company_name') or 'the operating company',
            'contact_email': (admin.email if admin else None) or 'support@invoiceiq.app',
        }

    @app.route('/privacy')
    def privacy_policy():
        return render_template('privacy.html', **_legal_doc_context())

    @app.route('/terms')
    def terms_of_use():
        return render_template('terms.html', **_legal_doc_context())

    @app.route('/invoices')
    def invoices():
        return render_template('invoices.html',
                               user_name=session.get('user_name'),
                               user_role=session.get('user_role', 'admin'))

    @app.route('/invoices/<int:invoice_id>')
    def invoice_detail(invoice_id):
        invoice = db.get_or_404(Invoice, invoice_id)
        return render_template('invoice_detail.html', invoice=invoice,
                               user_name=session.get('user_name'),
                               user_role=session.get('user_role', 'admin'))

    @app.route('/approvals')
    def approvals():
        return render_template('approvals.html',
                               user_name=session.get('user_name'),
                               user_role=session.get('user_role', 'admin'))

    @app.route('/settings')
    def settings_page():
        return render_template('settings.html',
                               user_name=session.get('user_name'),
                               user_role=session.get('user_role', 'admin'))

    # ------------------------------------------------------------------ #
    # OAuth routes — QuickBooks Online
    # ------------------------------------------------------------------ #

    @app.route('/auth/qbo/authorize')
    def qbo_authorize():
        if request.args.get('from') == 'wizard':
            session['oauth_return_to'] = 'wizard'
        state = secrets.token_urlsafe(24)
        session['oauth_state'] = state
        connector = get_connector()
        return redirect(connector.get_auth_url(state))

    @app.route('/auth/qbo/callback')
    def qbo_callback():
        code     = request.args.get('code', '')
        state    = request.args.get('state', '')
        realm_id = request.args.get('realmId', '')

        if state != session.pop('oauth_state', None):
            return "Invalid state parameter — possible CSRF attack.", 400

        return_to = session.pop('oauth_return_to', 'settings')
        try:
            connector = get_connector()
            connector.handle_callback(code, state, realmId=realm_id)
        except Exception as e:
            dest = url_for('wizard') if return_to == 'wizard' else url_for('settings_page')
            return redirect(dest + '?auth_error=' + str(e))

        if return_to == 'wizard':
            return redirect(url_for('wizard') + '?step=3&auth_success=qbo')
        return redirect(url_for('settings_page') + '?auth_success=qbo')

    # ------------------------------------------------------------------ #
    # OAuth routes — Xero
    # ------------------------------------------------------------------ #

    @app.route('/auth/xero/authorize')
    def xero_authorize():
        if request.args.get('from') == 'wizard':
            session['oauth_return_to'] = 'wizard'
        state = secrets.token_urlsafe(24)
        session['oauth_state'] = state
        connector = get_connector()
        return redirect(connector.get_auth_url(state))

    @app.route('/auth/xero/callback')
    def xero_callback():
        code  = request.args.get('code', '')
        state = request.args.get('state', '')

        if state != session.pop('oauth_state', None):
            return "Invalid state parameter — possible CSRF attack.", 400

        return_to = session.pop('oauth_return_to', 'settings')
        try:
            connector = get_connector()
            connector.handle_callback(code, state)
        except Exception as e:
            dest = url_for('wizard') if return_to == 'wizard' else url_for('settings_page')
            return redirect(dest + '?auth_error=' + str(e))

        if return_to == 'wizard':
            return redirect(url_for('wizard') + '?step=3&auth_success=xero')
        return redirect(url_for('settings_page') + '?auth_success=xero')

    # ------------------------------------------------------------------ #
    # API — Dashboard
    # ------------------------------------------------------------------ #

    @app.route('/api/dashboard/stats')
    def api_dashboard_stats():
        today = datetime.utcnow().date()

        rows   = db.session.query(Invoice.status, db.func.count(Invoice.id)) \
            .group_by(Invoice.status).all()
        counts = {s: c for s, c in rows}

        received_today = Invoice.query.filter(
            db.func.date(Invoice.email_received_at) == today
        ).count()

        pending    = sum(counts.get(s, 0) for s in
                         ('received', 'extracting', 'extracted', 'matching'))
        exceptions = counts.get('exception', 0) + counts.get('no_match', 0)
        recent     = Invoice.query.order_by(Invoice.created_at.desc()).limit(10).all()

        return jsonify({
            'received_today':     received_today,
            'awaiting_approval':  counts.get('awaiting_approval', 0),
            'exceptions':         exceptions,
            'pending_match':      pending,
            'ready_to_pay':       counts.get('ready_to_pay', 0),
            'total':              Invoice.query.count(),
            'status_counts':      counts,
            'recent':             [i.to_dict() for i in recent],
        })

    # ------------------------------------------------------------------ #
    # API — Invoices
    # ------------------------------------------------------------------ #

    @app.route('/api/invoices')
    def api_invoices():
        status          = request.args.get('status', '')
        search          = request.args.get('search', '').strip()
        needs_attention = request.args.get('needs_attention', '') == '1'
        page            = max(int(request.args.get('page', 1)), 1)
        per_page        = min(int(request.args.get('per_page', 25)), 100)

        q = Invoice.query
        if status:
            q = q.filter_by(status=status)
        if needs_attention:
            q = q.filter_by(push_failed=True)
        if search:
            q = q.filter(db.or_(
                Invoice.supplier_name.ilike(f'%{search}%'),
                Invoice.invoice_number.ilike(f'%{search}%'),
                Invoice.po_reference.ilike(f'%{search}%'),
                Invoice.email_from.ilike(f'%{search}%'),
            ))
        total = q.count()
        items = q.order_by(Invoice.created_at.desc()) \
            .offset((page - 1) * per_page).limit(per_page).all()

        return jsonify({
            'invoices': [i.to_dict() for i in items],
            'total':    total,
            'page':     page,
            'per_page': per_page,
            'pages':    max((total + per_page - 1) // per_page, 1),
        })

    @app.route('/api/invoices/upload', methods=['POST'])
    def api_upload_invoices():
        """Accept manually uploaded invoice files and run the full pipeline."""
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'error': 'No files provided'}), 400

        settings = load_settings()
        storage  = settings['app'].get('attachment_storage_path', 'invoices')
        Path(storage).mkdir(parents=True, exist_ok=True)

        ALLOWED = {'.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.webp'}
        results = []

        for f in files:
            if not f.filename:
                continue
            suffix = Path(f.filename).suffix.lower()
            if suffix not in ALLOWED:
                results.append({'filename': f.filename, 'error': 'Unsupported file type'})
                continue

            invoice = Invoice(
                email_message_id = f'upload_{uuid.uuid4().hex}',
                email_received_at = datetime.utcnow(),
                email_from        = 'manual_upload',
                email_subject     = f'Manual upload: {f.filename}',
                attachment_filename = f.filename,
                status            = 'received',
            )
            db.session.add(invoice)
            db.session.flush()

            safe_name = f'inv_{invoice.id}_{secure_filename(f.filename)}'
            save_path = str(Path(storage) / safe_name)
            f.save(save_path)
            invoice.attachment_path = save_path

            db.session.add(AuditLog(
                invoice_id = invoice.id,
                action     = 'received',
                user_name  = session.get('user_name', 'system'),
                notes      = f'Manually uploaded: {f.filename}',
            ))
            db.session.commit()

            # Run pipeline in background thread using app context
            def _run(app_ctx, inv_id):
                with app_ctx:
                    from src.invoice_processor import process_uploaded_invoice
                    process_uploaded_invoice(inv_id)

            threading.Thread(
                target=_run,
                args=(app.app_context(), invoice.id),
                daemon=True,
            ).start()

            results.append({'id': invoice.id, 'filename': f.filename})

        return jsonify({'invoices': results})

    # ------------------------------------------------------------------ #
    # Remittances (accounts receivable)
    # ------------------------------------------------------------------ #

    @app.route('/remittances')
    def remittances():
        return render_template('remittances.html',
                               user_name=session.get('user_name'),
                               user_role=session.get('user_role', 'admin'))

    @app.route('/api/remittances')
    def api_remittances():
        rows = Remittance.query.order_by(Remittance.created_at.desc()).limit(100).all()
        return jsonify({'remittances': [r.to_dict() for r in rows]})

    @app.route('/api/remittances/upload', methods=['POST'])
    def api_upload_remittances():
        """Accept manually uploaded remittance-advice files and run extraction +
        posting to the finance system in the background."""
        files = request.files.getlist('files')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'error': 'No files provided'}), 400

        settings = load_settings()
        storage  = settings['app'].get('attachment_storage_path', 'invoices')
        Path(storage).mkdir(parents=True, exist_ok=True)
        ALLOWED = {'.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif', '.webp'}
        results = []

        for f in files:
            if not f.filename:
                continue
            suffix = Path(f.filename).suffix.lower()
            if suffix not in ALLOWED:
                results.append({'filename': f.filename, 'error': 'Unsupported file type'})
                continue

            rem = Remittance(attachment_filename=f.filename, status='received')
            db.session.add(rem)
            db.session.flush()

            safe_name = f'remit_{rem.id}_{secure_filename(f.filename)}'
            save_path = str(Path(storage) / safe_name)
            f.save(save_path)
            rem.attachment_path = save_path
            db.session.commit()

            def _run(app_ctx, rem_id):
                with app_ctx:
                    from src.remittance_processor import process_remittance
                    process_remittance(rem_id)

            threading.Thread(target=_run, args=(app.app_context(), rem.id), daemon=True).start()
            results.append({'id': rem.id, 'filename': f.filename})

        return jsonify({'remittances': results})

    @app.route('/api/invoices/<int:invoice_id>')
    def api_invoice_detail(invoice_id):
        inv  = db.get_or_404(Invoice, invoice_id)
        data = inv.to_dict()

        data.update({
            'subtotal':                   float(inv.subtotal or 0),
            'vat_amount':                 float(inv.vat_amount or 0),
            'extraction_confidence_pct':  int((inv.extraction_confidence or 0) * 100),
            'match_confidence_pct':       int((inv.match_confidence or 0) * 100),
            'status_message':             inv.status_message,
            'rejection_reason':           inv.rejection_reason,
            'sage_transaction_ref':       inv.sage_transaction_ref,
            'supplier_ref':               inv.supplier_ref,
        })

        data['lines'] = [{
            'id': l.id, 'line_number': l.line_number,
            'description': l.description,
            'quantity':    float(l.quantity or 0),
            'unit_price':  float(l.unit_price or 0),
            'line_total':  float(l.line_total or 0),
            'vat_rate':    float(l.vat_rate or 0),
            'product_code': l.product_code,
            'matched':     l.matched,
        } for l in sorted(inv.lines, key=lambda x: x.line_number or 0)]

        data['audit_log'] = [{
            'action':    a.action,
            'user_name': a.user_name,
            'timestamp': a.timestamp.isoformat(),
            'notes':     a.notes,
        } for a in sorted(inv.audit_logs, key=lambda x: x.timestamp)]

        if inv.matched_po:
            po = inv.matched_po
            data['matched_po'] = {
                'id': po.id, 'po_number': po.po_number,
                'supplier_name': po.supplier_name,
                'total_amount':  float(po.total_amount or 0),
                'subtotal':      float(po.subtotal or 0),
                'vat_amount':    float(po.vat_amount or 0),
                'po_date':       po.po_date.isoformat() if po.po_date else None,
                'status':        po.status,
                'source':        po.source,
                'lines': [{
                    'line_number': l.line_number,
                    'description': l.description,
                    'quantity':    float(l.quantity or 0),
                    'unit_price':  float(l.unit_price or 0),
                    'line_total':  float(l.line_total or 0),
                    'product_code': l.product_code,
                } for l in sorted(po.lines, key=lambda x: x.line_number or 0)],
            }

        try:
            data['discrepancies'] = eval(inv.match_discrepancies) if inv.match_discrepancies else []
        except Exception:
            data['discrepancies'] = []

        if inv.assigned_approver:
            data['assigned_approver'] = inv.assigned_approver.to_dict()
        if inv.approved_by:
            data['approved_by'] = inv.approved_by.to_dict()

        return jsonify(data)

    @app.route('/api/invoices/<int:invoice_id>', methods=['PUT'])
    def api_update_invoice(invoice_id):
        """Let a reviewer correct AI-extracted fields/line items before posting."""
        inv = db.get_or_404(Invoice, invoice_id)
        if inv.status in ('ready_to_pay', 'rejected'):
            return jsonify({'error': f"Can't edit an invoice that's already {inv.status.replace('_', ' ')}"}), 400

        data = request.get_json() or {}

        def _num(value):
            if value in (None, ''):
                return None
            try:
                return round(float(str(value).replace('£', '').replace('$', '').replace(',', '').strip()), 2)
            except ValueError:
                return None

        if 'supplier_name' in data: inv.supplier_name = data['supplier_name'].strip() or None
        if 'supplier_ref'  in data: inv.supplier_ref  = data['supplier_ref'].strip() or None
        if 'invoice_number' in data: inv.invoice_number = data['invoice_number'].strip() or None
        if 'po_reference'  in data: inv.po_reference  = data['po_reference'].strip() or None
        if 'invoice_date' in data and data['invoice_date']:
            try:
                inv.invoice_date = datetime.strptime(data['invoice_date'][:10], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({'error': 'Invalid invoice date'}), 400
        if 'subtotal'     in data: inv.subtotal     = _num(data['subtotal'])
        if 'vat_amount'   in data: inv.vat_amount   = _num(data['vat_amount'])
        if 'total_amount' in data: inv.total_amount = _num(data['total_amount'])

        if 'lines' in data:
            kept_ids = set()
            for line_data in data['lines']:
                line_id = line_data.get('id')
                line = InvoiceLine.query.get(line_id) if line_id else None
                if not line or line.invoice_id != inv.id:
                    line = InvoiceLine(invoice_id=inv.id)
                    db.session.add(line)
                line.description  = (line_data.get('description') or '').strip() or None
                line.product_code = (line_data.get('product_code') or '').strip() or None
                line.quantity      = _num(line_data.get('quantity')) or 0
                line.unit_price    = _num(line_data.get('unit_price')) or 0
                line.line_total    = _num(line_data.get('line_total')) or 0
                line.vat_rate       = _num(line_data.get('vat_rate')) or 0
                db.session.flush()
                kept_ids.add(line.id)
            InvoiceLine.query.filter(
                InvoiceLine.invoice_id == inv.id, ~InvoiceLine.id.in_(kept_ids or [0])
            ).delete(synchronize_session=False)

        db.session.add(AuditLog(
            invoice_id=inv.id, action='details_corrected',
            user_name=session.get('user_name', 'system'),
            notes='Extracted data manually corrected before approval'
        ))
        db.session.commit()
        return jsonify({'success': True})

    # ------------------------------------------------------------------ #
    # API — Approval actions
    # ------------------------------------------------------------------ #

    @app.route('/api/invoices/<int:invoice_id>/approve', methods=['POST'])
    def api_approve(invoice_id):
        inv    = db.get_or_404(Invoice, invoice_id)
        notes  = (request.get_json() or {}).get('notes', '')
        wf     = ApprovalWorkflow()
        if not wf.approve(inv, _user_name(), notes):
            return jsonify({'error': 'Cannot approve invoice in its current state'}), 400

        settings = load_settings()
        if inv.supplier_ref:
            try:
                _post_to_finance(inv)
            except Exception as e:
                inv.status_message = f"Approved — finance post failed: {e}"

        # The mirror-to-LedgerIQ integration is for when a *different* system
        # (Sage/QBO/Xero) is primary. When Ledger-IQ IS the finance system the
        # connector already posted above — don't post a second time.
        if (settings.get('integrations', {}).get('ledgeriq', {}).get('enabled')
                and settings.get('finance_system') != 'ledgeriq'):
            try:
                _post_to_ledgeriq(inv, settings)
            except Exception as e:
                app.logger.error(f"LedgerIQ post failed for invoice {inv.id}: {e}")
                note = f"Ledger-IQ post failed: {_friendly_ledgeriq_error(e)}"
                inv.status_message = f"{inv.status_message} | {note}" if inv.status_message else note

        _recompute_push_failed(inv, settings)
        db.session.commit()

        return jsonify({'success': True, 'status': inv.status, 'push_failed': inv.push_failed})

    @app.route('/api/invoices/<int:invoice_id>/reject', methods=['POST'])
    def api_reject(invoice_id):
        inv    = db.get_or_404(Invoice, invoice_id)
        reason = (request.get_json() or {}).get('reason', '').strip()
        if not reason:
            return jsonify({'error': 'Rejection reason required'}), 400
        wf = ApprovalWorkflow()
        if not wf.reject(inv, _user_name(), reason):
            return jsonify({'error': 'Cannot reject invoice in its current state'}), 400
        return jsonify({'success': True, 'status': inv.status})

    @app.route('/api/invoices/<int:invoice_id>/assign-approver', methods=['POST'])
    def api_assign_approver(invoice_id):
        inv     = db.get_or_404(Invoice, invoice_id)
        user_id = (request.get_json() or {}).get('user_id')
        user    = db.session.get(User, user_id) if user_id else None
        if not user or not user.is_active:
            return jsonify({'error': 'Invalid or inactive user'}), 400

        inv.assigned_approver_id = user.id
        db.session.add(AuditLog(
            invoice_id=inv.id, action='approver_reassigned',
            user_name=session.get('user_name', 'system'),
            notes=f'Assigned to {user.name}'
        ))
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/invoices/<int:invoice_id>/post-to-finance', methods=['POST'])
    def api_post_to_finance(invoice_id):
        inv = db.get_or_404(Invoice, invoice_id)
        settings = load_settings()
        try:
            _post_to_finance(inv)
            inv.status_message = None
            _recompute_push_failed(inv, settings)
            db.session.commit()
            return jsonify({'success': True, 'status': inv.status,
                            'transaction_ref': inv.sage_transaction_ref,
                            'push_failed': inv.push_failed})
        except Exception as e:
            inv.status_message = f"Finance post failed: {e}"
            _recompute_push_failed(inv, settings)
            db.session.commit()
            return jsonify({'error': str(e)}), 500

    @app.route('/api/invoices/<int:invoice_id>/post-to-ledgeriq', methods=['POST'])
    def api_post_to_ledgeriq(invoice_id):
        inv = db.get_or_404(Invoice, invoice_id)
        settings = load_settings()
        if not settings.get('integrations', {}).get('ledgeriq', {}).get('enabled'):
            return jsonify({'error': 'Ledger-IQ integration is not enabled in Settings'}), 400
        try:
            _post_to_ledgeriq(inv, settings)
            inv.status_message = None
            _recompute_push_failed(inv, settings)
            db.session.commit()
            return jsonify({'success': True, 'push_failed': inv.push_failed})
        except Exception as e:
            friendly = _friendly_ledgeriq_error(e)
            inv.status_message = f"Ledger-IQ post failed: {friendly}"
            _recompute_push_failed(inv, settings)
            db.session.commit()
            return jsonify({'error': friendly}), 500

    @app.route('/api/invoices/<int:invoice_id>', methods=['DELETE'])
    def api_delete_invoice(invoice_id):
        inv = db.get_or_404(Invoice, invoice_id)
        if inv.attachment_path:
            try:
                Path(inv.attachment_path).unlink(missing_ok=True)
            except Exception:
                pass
        db.session.delete(inv)  # cascades to InvoiceLine and AuditLog
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/approvals')
    def api_approvals():
        uid = _user_id()
        if uid:
            tasks = ApprovalWorkflow().get_pending_tasks(uid)
        else:
            tasks = Invoice.query.filter_by(status='awaiting_approval') \
                .order_by(Invoice.created_at.desc()).all()
        return jsonify({'tasks': [i.to_dict() for i in tasks]})

    @app.route('/api/approval-count')
    def api_approval_count():
        uid = _user_id()
        count = Invoice.query.filter_by(
            status='awaiting_approval',
            **(dict(assigned_approver_id=uid) if uid else {})
        ).count()
        return jsonify({'count': count})

    @app.route('/api/needs-attention-count')
    def api_needs_attention_count():
        count = Invoice.query.filter_by(push_failed=True).count()
        return jsonify({'count': count})

    # ------------------------------------------------------------------ #
    # API — Email / PO sync
    # ------------------------------------------------------------------ #

    @app.route('/api/email/poll', methods=['POST'])
    def api_poll_email():
        try:
            from src.invoice_processor import process_new_emails
            stats = process_new_emails()
            return jsonify({'success': True, **stats})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/finance/sync-pos', methods=['POST'])
    def api_sync_pos():
        settings = load_settings()
        source   = settings['po_source'].get('type', 'connector')
        try:
            if source == 'connector':
                from src.invoice_processor import sync_pos_from_connector
                count = sync_pos_from_connector()
            else:
                from src.invoice_processor import sync_pos_from_folder
                count = sync_pos_from_folder()
            return jsonify({'success': True, 'synced': count})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/finance/expense-accounts')
    def api_expense_accounts():
        try:
            return jsonify({'accounts': get_connector().get_expense_accounts()})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ------------------------------------------------------------------ #
    # API — Auth status (for settings page)
    # ------------------------------------------------------------------ #

    @app.route('/api/auth/status')
    def api_auth_status():
        settings = load_settings()
        system   = settings.get('finance_system', 'sage')

        if system == 'sage':
            return jsonify({'system': 'sage', 'requires_oauth': False,
                            'authenticated': True})

        try:
            connector = get_connector(settings)
            auth      = connector.is_authenticated()
            msg       = ''
            if auth:
                ok, msg = connector.test_connection()
            return jsonify({
                'system':          system,
                'requires_oauth':  True,
                'authenticated':   auth,
                'connection_info': msg if auth else '',
            })
        except Exception as e:
            return jsonify({'system': system, 'requires_oauth': True,
                            'authenticated': False, 'error': str(e)})

    @app.route('/api/auth/disconnect', methods=['POST'])
    def api_auth_disconnect():
        try:
            get_connector().disconnect()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    # ------------------------------------------------------------------ #
    # API — Settings
    # ------------------------------------------------------------------ #

    @app.route('/api/settings', methods=['GET'])
    def api_get_settings():
        s    = load_settings()
        safe = json.loads(json.dumps(s))
        _mask(safe, ('email', 'client_secret'))
        _mask(safe, ('sage',  'password'))
        _mask(safe, ('qbo',   'client_secret'))
        _mask(safe, ('xero',  'client_secret'))
        _mask(safe, ('ledgeriq', 'api_key'))
        _mask(safe, ('claude','api_key'))
        if safe.get('integrations', {}).get('ledgeriq', {}).get('api_key'):
            safe['integrations']['ledgeriq']['api_key'] = '••••••••'
        return jsonify(safe)

    @app.route('/api/settings', methods=['POST'])
    def api_save_settings():
        new     = request.get_json()
        current = load_settings()
        # Preserve masked values
        for path in (('email', 'client_secret'), ('sage', 'password'),
                     ('qbo', 'client_secret'), ('xero', 'client_secret'),
                     ('ledgeriq', 'api_key'), ('claude', 'api_key')):
            if new.get(path[0], {}).get(path[1]) == '••••••••':
                new[path[0]][path[1]] = current[path[0]].get(path[1], '')
        if new.get('integrations', {}).get('ledgeriq', {}).get('api_key') == '••••••••':
            new['integrations']['ledgeriq']['api_key'] = \
                current.get('integrations', {}).get('ledgeriq', {}).get('api_key', '')
        _sync_approver_users(new)          # hash any approver passwords into User table first
        _strip_approver_passwords(new)     # then strip plaintext before persisting to disk
        save_settings(new)
        return jsonify({'success': True})

    @app.route('/api/settings/partial', methods=['POST'])
    def api_settings_partial():
        """Merge a partial settings dict into stored settings (used by wizard)."""
        patch   = request.get_json() or {}
        current = load_settings()
        from src.config_manager import _deep_merge
        _deep_merge(current, patch)
        # Preserve masked secret placeholders
        for path in (('email', 'client_secret'), ('sage', 'password'),
                     ('qbo', 'client_secret'), ('xero', 'client_secret'),
                     ('ledgeriq', 'api_key'), ('claude', 'api_key')):
            if current.get(path[0], {}).get(path[1]) == '••••••••':
                current[path[0]][path[1]] = ''
        _sync_approver_users(current)
        _strip_approver_passwords(current)
        save_settings(current)
        return jsonify({'success': True})

    @app.route('/api/wizard/create-admin', methods=['POST'])
    def api_wizard_create_admin():
        """Create (or update) the first admin account and log them in.

        Called right after wizard Step 1, before the user navigates away for
        QBO/Xero OAuth — that round-trip is a full page reload that wipes any
        unsaved form state, so the account must exist (and the session be
        set) before that happens, not at the very end of the wizard.
        """
        if load_settings().get('app', {}).get('setup_complete'):
            return jsonify({'error': 'Setup has already been completed.'}), 400

        data     = request.get_json() or {}
        email    = data.get('admin_email', '').strip().lower()
        password = data.get('admin_password', '')
        name     = data.get('admin_name', '').strip()

        if not email or not password:
            return jsonify({'error': 'Admin email and password are required'}), 400
        if len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters.'}), 400

        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(email=email, name=name or 'Admin')
            db.session.add(user)
        user.name      = name or user.name or 'Admin'
        user.role      = 'admin'
        user.is_active = True
        user.set_password(password)
        db.session.commit()

        session.update({'user_id': user.id, 'user_name': user.name, 'user_role': user.role})
        return jsonify({'success': True})

    @app.route('/api/wizard/complete', methods=['POST'])
    def api_wizard_complete():
        """Mark setup as done and apply the final wizard settings payload.
        The admin account is created earlier, in /api/wizard/create-admin."""
        payload = request.get_json() or {}
        payload.pop('admin_email', None)
        payload.pop('admin_password', None)
        payload.pop('admin_name', None)

        if not session.get('user_id'):
            return jsonify({'error': 'Admin account was not created — please go back to Step 1.'}), 400

        current = load_settings()
        from src.config_manager import _deep_merge
        _deep_merge(current, payload)
        current.setdefault('app', {})['setup_complete'] = True
        _sync_approver_users(current)
        _strip_approver_passwords(current)
        save_settings(current)
        return jsonify({'success': True})

    @app.route('/api/account/change-password', methods=['POST'])
    def api_change_password():
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': 'No account associated with this session.'}), 400
        user = db.session.get(User, user_id)
        if not user:
            return jsonify({'error': 'User not found.'}), 404

        data     = request.get_json() or {}
        current  = data.get('current_password', '')
        new_pass = data.get('new_password', '')

        if not user.check_password(current):
            return jsonify({'error': 'Current password is incorrect.'}), 400
        if len(new_pass) < 8:
            return jsonify({'error': 'New password must be at least 8 characters.'}), 400

        user.set_password(new_pass)
        user.must_change_password = False
        db.session.commit()
        return jsonify({'success': True})

    @app.route('/api/settings/test-connection', methods=['POST'])
    def api_test_connection():
        try:
            ok, msg = get_connector().test_connection()
            return jsonify({'success': ok, 'message': msg})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

    @app.route('/api/settings/test-email', methods=['POST'])
    def api_test_email():
        try:
            from src.email_monitor import EmailMonitor
            ok, msg = EmailMonitor().test_connection()
            return jsonify({'success': ok, 'message': msg})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

    @app.route('/api/invoices/<int:invoice_id>/attachment')
    def api_attachment(invoice_id):
        inv = db.get_or_404(Invoice, invoice_id)
        if inv.attachment_path and Path(inv.attachment_path).exists():
            return send_file(inv.attachment_path)
        return jsonify({'error': 'Attachment not found'}), 404

    @app.route('/api/users')
    def api_users():
        users = User.query.order_by(User.name).all()
        return jsonify({'users': [u.to_dict() for u in users]})

    @app.route('/api/users', methods=['POST'])
    def api_create_user():
        data     = request.get_json() or {}
        name     = data.get('name', '').strip()
        email    = data.get('email', '').strip().lower()
        password = data.get('password', '')
        role     = data.get('role', 'approver')

        if not name or not email:
            return jsonify({'error': 'Name and email are required'}), 400
        if role not in ('admin', 'approver', 'viewer'):
            return jsonify({'error': 'Invalid role'}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'A user with that email already exists'}), 400
        if not password or len(password) < 8:
            return jsonify({'error': 'Password must be at least 8 characters'}), 400

        user = User(name=name, email=email, role=role, is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return jsonify({'success': True, 'user': user.to_dict()})

    @app.route('/api/users/<int:user_id>', methods=['PUT'])
    def api_update_user(user_id):
        user = db.get_or_404(User, user_id)
        data = request.get_json() or {}

        if 'name' in data and data['name'].strip():
            user.name = data['name'].strip()
        if 'role' in data:
            if data['role'] not in ('admin', 'approver', 'viewer'):
                return jsonify({'error': 'Invalid role'}), 400
            if user.id == session.get('user_id') and data['role'] != 'admin':
                return jsonify({'error': "You can't remove your own admin role"}), 400
            user.role = data['role']
        if 'is_active' in data:
            if user.id == session.get('user_id') and not data['is_active']:
                return jsonify({'error': "You can't deactivate your own account"}), 400
            user.is_active = bool(data['is_active'])
        if data.get('password'):
            if len(data['password']) < 8:
                return jsonify({'error': 'Password must be at least 8 characters'}), 400
            user.set_password(data['password'])

        db.session.commit()
        return jsonify({'success': True, 'user': user.to_dict()})

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _send_password_reset_email(user: User, temp_password: str):
        api_key = os.environ.get('RESEND_API_KEY')
        if not api_key:
            app.logger.warning("RESEND_API_KEY not configured — skipping password reset email")
            return
        login_url = f"{os.environ.get('APP_URL', 'https://invoice.sol-iq.co.uk')}/login"
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": os.environ.get('EMAIL_FROM', 'Invoice-IQ <no-reply@sol-iq.co.uk>'),
                "to": user.email,
                "subject": "Your Invoice-IQ password has been reset",
                "html": f"""
                  <div style="font-family:Arial,Helvetica,sans-serif;color:#1e293b;max-width:480px;margin:0 auto;">
                    <h2 style="margin:0 0 12px;">Password reset, {user.name}</h2>
                    <p style="font-size:14px;color:#475569;">
                      You (or someone on your behalf) requested a password reset for Invoice-IQ.
                      Use the temporary password below to log in — you'll be asked to set your own password right away.
                    </p>
                    <div style="margin:20px 0;padding:16px;background:#f8fafc;border-radius:8px;font-size:14px;">
                      <p style="margin:2px 0;">Email: <strong>{user.email}</strong></p>
                      <p style="margin:2px 0;">Temporary password: <strong style="font-family:monospace;">{temp_password}</strong></p>
                    </div>
                    <a href="{login_url}" style="display:inline-block;padding:10px 20px;background:#F5A623;color:#171A2B;font-weight:600;border-radius:8px;text-decoration:none;font-size:14px;">
                      Log in to Invoice-IQ
                    </a>
                    <p style="margin-top:24px;font-size:12px;color:#94a3b8;">
                      If you didn't request this, please contact your administrator immediately.
                    </p>
                  </div>""",
            },
            timeout=10,
        )
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")

    def _friendly_ledgeriq_error(e: Exception) -> str:
        """Translate LedgerIQ's raw API error text into something a user can act on."""
        msg = str(e)
        if 'Unique constraint failed' in msg and 'invoiceNumber' in msg:
            return ("LedgerIQ already has an invoice with this invoice number for this supplier/direction. "
                    "Edit the invoice number on this invoice, then retry.")
        if 'No expense category configured' in msg:
            return "LedgerIQ has no expense category set up for this organisation — add one in LedgerIQ under Categories, then retry."
        if 'Invalid API key' in msg:
            return "LedgerIQ rejected the API key — check it in Settings."
        if 'invalid_type' in msg or 'Invalid payload' in msg:
            return f"LedgerIQ rejected the data sent (validation error): {msg}"
        return f"LedgerIQ error: {msg}"

    def _recompute_push_failed(inv: Invoice, settings: dict):
        """Re-derive the push_failed flag from actual outcomes, since finance and
        LedgerIQ pushes are independent and either can fail/succeed on its own."""
        finance_needed = bool(inv.supplier_ref or inv.supplier_name)
        finance_ok = bool(inv.sage_transaction_ref)

        ledgeriq_needed = settings.get('integrations', {}).get('ledgeriq', {}).get('enabled', False)
        ledgeriq_ok = bool(AuditLog.query.filter_by(invoice_id=inv.id, action='posted_to_ledgeriq').first())

        inv.push_failed = (finance_needed and not finance_ok) or (ledgeriq_needed and not ledgeriq_ok)

    def _post_to_finance(inv: Invoice):
        connector = get_connector()

        # Vendor lookup may have failed when the invoice was first processed
        # (e.g. the finance connection was broken at the time) — retry it
        # now rather than silently posting a null VendorRef, which QuickBooks
        # rejects with an unhelpful generic validation error.
        if not inv.supplier_ref and inv.supplier_name:
            try:
                found = connector.find_vendor(inv.supplier_name)
            except Exception as e:
                raise ValueError(f"{get_system_name()} vendor lookup failed: {e}")
            if found:
                inv.supplier_ref = found
                db.session.commit()

        if not inv.supplier_ref:
            raise ValueError(
                f"No {get_system_name()} vendor matches '{inv.supplier_name}'. "
                f"Create the vendor in {get_system_name()} first, or check the supplier name is correct."
            )

        ref = connector.post_invoice({
            'external_id':    inv.id,
            'supplier_name':  inv.supplier_name,
            'supplier_ref':   inv.supplier_ref,
            'invoice_number': inv.invoice_number,
            'invoice_date':   inv.invoice_date,
            'po_reference':   inv.po_reference,
            'currency':       inv.currency,
            'subtotal':       float(inv.subtotal or 0),
            'vat_amount':     float(inv.vat_amount or 0),
            'total_amount':   float(inv.total_amount or 0),
            'lines': [{
                'vat_rate':    float(l.vat_rate or 0),
                'description': l.description,
                'quantity':    float(l.quantity or 0),
                'unit_price':  float(l.unit_price or 0),
                'line_total':  float(l.line_total or 0),
            } for l in inv.lines],
        })
        inv.sage_transaction_ref = ref
        inv.posted_to_sage_at   = datetime.utcnow()
        inv.status = 'ready_to_pay'
        db.session.add(AuditLog(
            invoice_id=inv.id, action='posted_to_finance',
            user_name=session.get('user_name', 'system'),
            notes=f"Posted to {get_system_name()} — ref: {ref}"
        ))
        db.session.commit()

    def _post_to_ledgeriq(inv: Invoice, settings: dict):
        """Push an approved supplier invoice into LedgerIQ as a purchase bill.
        Best-effort — failures here are logged but never block approval, since
        LedgerIQ bookkeeping is a convenience mirror, not the system of record
        this app posts to (that's Sage/QBO/Xero via _post_to_finance)."""
        cfg = settings.get('integrations', {}).get('ledgeriq', {})
        api_key = cfg.get('api_key')
        base_url = (cfg.get('api_base_url') or '').rstrip('/')
        if not api_key or not base_url:
            raise ValueError("LedgerIQ integration is enabled but missing an API key or base URL")

        payload = {
            'externalId':     str(inv.id),
            'supplierName':   inv.supplier_name,
            'supplierRef':    inv.supplier_ref or None,
            'invoiceNumber':  inv.invoice_number or f"AP-{inv.id}",
            'invoiceDate':    inv.invoice_date.isoformat() if inv.invoice_date else datetime.utcnow().date().isoformat(),
            'poReference':    inv.po_reference or None,
            'lines': [{
                'description': l.description or '(no description)',
                'quantity':    float(l.quantity or 1),
                'unitPrice':   float(l.unit_price or 0),
                'lineTotal':   float(l.line_total or 0),
                'vatRate':     float(l.vat_rate or 0),
            } for l in inv.lines] or [{
                'description': inv.invoice_number or 'Invoice',
                'quantity':    1,
                'unitPrice':   float(inv.subtotal or 0),
                'lineTotal':   float(inv.subtotal or 0),
                'vatRate':     0,
            }],
        }

        resp = requests.post(
            f"{base_url}/api/v1/purchase-invoices",
            headers={
                'Authorization': f"Bearer {api_key}",
                'Accept': 'application/json',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text[:500]}")

        result = resp.json()
        db.session.add(AuditLog(
            invoice_id=inv.id, action='posted_to_ledgeriq',
            user_name=session.get('user_name', 'system'),
            notes=f"Posted to LedgerIQ — invoice id: {result.get('invoiceId')}"
                  + (" (new supplier created)" if result.get('supplierCreated') else "")
        ))
        db.session.commit()

    def _mask(d: dict, path: tuple):
        section, key = path
        if d.get(section, {}).get(key):
            d[section][key] = '••••••••'

    def _strip_approver_passwords(settings: dict):
        """Remove plaintext passwords before settings are written to disk —
        they're hashed into the User table by _sync_approver_users instead."""
        for a in settings.get('approval', {}).get('approvers', []):
            a.pop('password', None)

    def _sync_approver_users(settings: dict):
        for a in settings.get('approval', {}).get('approvers', []):
            email    = a.get('email', '').strip().lower()
            name     = a.get('name', '').strip()
            password = a.get('password', '').strip()
            if not email or not name:
                continue
            user = User.query.filter_by(email=email).first()
            if not user:
                user = User(email=email, name=name, role='approver')
                db.session.add(user)
            else:
                user.name = name
            if password:
                user.set_password(password)
        db.session.commit()

    # Start scheduler
    try:
        from src.scheduler import init_scheduler
        init_scheduler(app)
    except Exception as e:
        app.logger.warning(f"Scheduler not started: {e}")

    return app


if __name__ == '__main__':
    application = create_app()
    settings    = load_settings()
    port        = int(settings['app'].get('port', 5000))
    application.run(host='127.0.0.1', port=port, debug=False)
