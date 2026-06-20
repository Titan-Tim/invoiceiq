import json
import os
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path

from werkzeug.utils import secure_filename

from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, send_file)

from src.database import db, Invoice, InvoiceLine, PurchaseOrder, POLine, User, AuditLog
from src.config_manager import load_settings, save_settings
from src.approval import ApprovalWorkflow
from src.connectors.factory import get_connector, get_system_name


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
        return {
            'finance_system_name': get_system_name(settings),
            'finance_system_key':  settings.get('finance_system', 'sage'),
        }

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _user_name():  return session.get('user_name', 'System')
    def _user_id():    return session.get('user_id')

    PUBLIC_ENDPOINTS = {'login', 'healthz', 'static'}
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
        settings = load_settings()
        if settings.get('app', {}).get('setup_complete'):
            return redirect(url_for('dashboard'))
        step = request.args.get('step', '1')
        auth_result = {
            'success': request.args.get('auth_success', ''),
            'error':   request.args.get('auth_error',   ''),
        }
        return render_template('wizard.html', current_step=step, auth_result=auth_result)

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
                return redirect(url_for('dashboard'))
            error = 'Incorrect email or password.'
        return render_template('login.html', error=error)

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

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
        status   = request.args.get('status', '')
        search   = request.args.get('search', '').strip()
        page     = max(int(request.args.get('page', 1)), 1)
        per_page = min(int(request.args.get('per_page', 25)), 100)

        q = Invoice.query
        if status:
            q = q.filter_by(status=status)
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
                db.session.commit()

        return jsonify({'success': True, 'status': inv.status})

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

    @app.route('/api/invoices/<int:invoice_id>/post-to-finance', methods=['POST'])
    def api_post_to_finance(invoice_id):
        inv = db.get_or_404(Invoice, invoice_id)
        try:
            _post_to_finance(inv)
            return jsonify({'success': True, 'status': inv.status,
                            'transaction_ref': inv.sage_transaction_ref})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

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
        _mask(safe, ('claude','api_key'))
        return jsonify(safe)

    @app.route('/api/settings', methods=['POST'])
    def api_save_settings():
        new     = request.get_json()
        current = load_settings()
        # Preserve masked values
        for path in (('email', 'client_secret'), ('sage', 'password'),
                     ('qbo', 'client_secret'), ('xero', 'client_secret'),
                     ('claude', 'api_key')):
            if new.get(path[0], {}).get(path[1]) == '••••••••':
                new[path[0]][path[1]] = current[path[0]].get(path[1], '')
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
                     ('claude', 'api_key')):
            if current.get(path[0], {}).get(path[1]) == '••••••••':
                current[path[0]][path[1]] = ''
        _sync_approver_users(current)
        _strip_approver_passwords(current)
        save_settings(current)
        return jsonify({'success': True})

    @app.route('/api/wizard/complete', methods=['POST'])
    def api_wizard_complete():
        """Create the admin account, mark setup as done, apply final wizard payload."""
        payload         = request.get_json() or {}
        admin_email     = payload.pop('admin_email', '').strip().lower()
        admin_password  = payload.pop('admin_password', '')
        admin_name      = payload.pop('admin_name', '').strip()

        if not admin_email or not admin_password:
            return jsonify({'error': 'Admin email and password are required'}), 400

        current = load_settings()
        from src.config_manager import _deep_merge
        _deep_merge(current, payload)
        current.setdefault('app', {})['setup_complete'] = True
        _sync_approver_users(current)
        _strip_approver_passwords(current)
        save_settings(current)

        user = User.query.filter_by(email=admin_email).first()
        if not user:
            user = User(email=admin_email, name=admin_name or 'Admin')
            db.session.add(user)
        user.name      = admin_name or user.name or 'Admin'
        user.role      = 'admin'
        user.is_active = True
        user.set_password(admin_password)
        db.session.commit()

        session.update({'user_id': user.id, 'user_name': user.name, 'user_role': user.role})
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
        users = User.query.filter_by(is_active=True).order_by(User.name).all()
        return jsonify({'users': [u.to_dict() for u in users]})

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _post_to_finance(inv: Invoice):
        connector = get_connector()
        ref = connector.post_invoice({
            'supplier_ref':   inv.supplier_ref,
            'invoice_number': inv.invoice_number,
            'invoice_date':   inv.invoice_date,
            'po_reference':   inv.po_reference,
            'subtotal':       float(inv.subtotal or 0),
            'vat_amount':     float(inv.vat_amount or 0),
            'total_amount':   float(inv.total_amount or 0),
            'lines': [{
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
