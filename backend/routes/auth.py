"""Blueprint: auth routes, extracted from app.py.

Auto-extracted 2026-09-15 as part of the backend modularity pass -- shared
helpers/caches/table clients stay in app.py (imported as `app` and referenced
as app.<name> throughout, matching the exact late-binding lookup semantics
the code relied on when these functions lived in app.py directly, so
test-time monkeypatching of app.<name> globals still works unchanged).
"""
from flask import Blueprint

import app

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/auth/config', methods=['GET'])
@auth_bp.route('/api/auth/config', methods=['GET'])
def auth_config():
    """Public: what the sign-in UI needs to render (no secrets)."""
    return app.jsonify({
        'authMode': app.AUTH_MODE,
        'authRequired': app.AUTH_REQUIRED,
        'passwordResetAvailable': app.AUTH_MODE == 'password' and app.email_utils.is_configured(),
    })

@auth_bp.route('/auth/login', methods=['POST'])
@auth_bp.route('/api/auth/login', methods=['POST'])
def auth_login():
    guard = app._password_mode_guard()
    if guard:
        return guard
    data = app.request.get_json(silent=True) or {}
    email_in = app.library_utils.normalize_email(data.get('email'))
    password = str(data.get('password', '') or '')
    if not email_in or not password:
        return app.jsonify({'error': 'Email and password are required.'}), 400
    # Brute-force protection is scoped per email so one targeted account can't
    # lock everyone else out of a shared deployment.
    throttle_row = f'login-throttle:{email_in}'
    if not app.password_auth.login_attempt_allowed(app.config_table_client, throttle_row):
        return app.jsonify({'error': 'Too many attempts. Please wait and try again.'}), 429
    account = app.library_store.get_user_by_email(email_in) if app.library_store else None
    stored_hash = str((account or {}).get('passwordHash') or '')
    if not account or not stored_hash or not app.password_auth.verify_password(password, stored_hash):
        app.password_auth.record_login_failure(app.config_table_client, row_key=throttle_row)
        return app.jsonify({'error': 'Incorrect email or password.'}), 401
    app.password_auth.record_login_success(app.config_table_client, throttle_row)
    uid = str(account.get('RowKey'))
    email = str(account.get('email') or email_in)
    library_id = app.library_store.get_current_library(uid) if app.library_store else uid
    token = app._issue_session_for(uid, library_id=library_id, email=email, mode='password')
    return app.jsonify({'token': token, 'email': email, 'expiresIn': app.SESSION_TTL_SECONDS})

@auth_bp.route('/auth/exchange', methods=['POST'])
@auth_bp.route('/api/auth/exchange', methods=['POST'])
def auth_exchange():
    """Entra mode: exchange a validated Microsoft access token for a Photostore
    session token that carries the active library + token version. We can't stamp
    those claims into Microsoft's token, so both modes converge on a token we
    sign. Called once by the SPA after MSAL sign-in."""
    if app.AUTH_MODE != 'entra':
        return app.jsonify({'error': 'Token exchange is only available in Entra mode.'}), 400
    auth_header = str(app.request.headers.get('Authorization', '') or '')
    if not auth_header.lower().startswith('bearer '):
        return app.jsonify({'error': 'Authorization token is required.'}), 401
    ms_token = auth_header.split(' ', 1)[1].strip()
    try:
        payload = app.validate_entra_bearer_token(
            ms_token, app.AZURE_AD_TENANT_ID, app.AZURE_AD_CLIENT_ID, app.AZURE_AD_API_AUDIENCE,
        )
    except Exception as exc:
        app.app.logger.warning('Microsoft token validation failed: %s', exc)
        return app.jsonify({'error': 'Invalid Microsoft token'}), 401
    user_id = str(payload.get('oid') or payload.get('sub') or payload.get('preferred_username') or '').strip()
    if not user_id:
        return app.jsonify({'error': 'Token does not contain a usable user identifier claim.'}), 401
    email = str(payload.get('preferred_username') or payload.get('email') or payload.get('upn') or '').strip()
    app._ensure_account_bootstrapped(user_id, email=email)
    library_id = app.library_store.get_current_library(user_id) if app.library_store else user_id
    token = app._issue_session_for(user_id, library_id=library_id, email=email, mode='entra')
    return app.jsonify({'token': token, 'email': email, 'expiresIn': app.SESSION_TTL_SECONDS})

@auth_bp.route('/auth/change-password', methods=['POST'])
@auth_bp.route('/api/auth/change-password', methods=['POST'])
def auth_change_password():
    guard = app._password_mode_guard()
    if guard:
        return guard
    account_id, library_id, error = app._require_library_context(require_auth=True)
    if error:
        return error
    data = app.request.get_json(silent=True) or {}
    current = str(data.get('currentPassword', '') or '')
    new_password = str(data.get('newPassword', '') or '')
    if len(new_password) < 8:
        return app.jsonify({'error': 'New password must be at least 8 characters.'}), 400
    account = app.library_store.get_user(account_id) if app.library_store else None
    stored_hash = str((account or {}).get('passwordHash') or '')
    if not account or not stored_hash or not app.password_auth.verify_password(current, stored_hash):
        return app.jsonify({'error': 'Current password is incorrect.'}), 401
    app.library_store.set_user_password(account_id, app.password_auth.hash_password(new_password))
    # Session-kill: invalidate every outstanding token, then hand this session a
    # fresh one so the user who just changed their password stays signed in here,
    # in the same library they were in (not reset to their own).
    app.library_store.bump_token_version(account_id)
    token = app._issue_session_for(account_id, library_id=library_id, email=str(account.get('email') or ''), mode='password')
    return app.jsonify({'status': 'ok', 'token': token, 'expiresIn': app.SESSION_TTL_SECONDS})

@auth_bp.route('/auth/forgot', methods=['POST'])
@auth_bp.route('/api/auth/forgot', methods=['POST'])
def auth_forgot():
    guard = app._password_mode_guard()
    if guard:
        return guard
    # Always return success to avoid revealing whether an account exists for the
    # supplied address (no account enumeration).
    data = app.request.get_json(silent=True) or {}
    email_in = app.library_utils.normalize_email(data.get('email'))
    generic = app.jsonify({'status': 'ok'})
    if not app.email_utils.is_configured() or not email_in or app.library_store is None:
        return generic
    account = app.library_store.get_user_by_email(email_in)
    if not account:
        return generic
    account_id = str(account.get('RowKey'))
    # Throttle the unauthenticated send path (per account) so it can't be used to
    # email-bomb a user or burn ACS email quota. Still return the same 200.
    if not app.library_store.reset_email_allowed(account_id):
        return generic
    try:
        raw_token = app.library_store.create_reset_token(account_id, ttl_seconds=3600)
        base = (app.PUBLIC_APP_BASE_URL or '').rstrip('/')
        reset_url = f'{base}/reset-password?token={raw_token}'
        app.email_utils.send_password_reset_email(str(account.get('email') or email_in), reset_url)
    except Exception as exc:
        app.app.logger.warning('Password reset email failed: %s', exc)
    return generic

@auth_bp.route('/auth/reset', methods=['POST'])
@auth_bp.route('/api/auth/reset', methods=['POST'])
def auth_reset():
    guard = app._password_mode_guard()
    if guard:
        return guard
    data = app.request.get_json(silent=True) or {}
    token = str(data.get('token', '') or '')
    new_password = str(data.get('newPassword', '') or '')
    if len(new_password) < 8:
        return app.jsonify({'error': 'New password must be at least 8 characters.'}), 400
    user_id = app.library_store.consume_reset_token(token) if app.library_store else None
    if not user_id:
        return app.jsonify({'error': 'This reset link is invalid or has expired. Please request a new one.'}), 400
    app.library_store.set_user_password(user_id, app.password_auth.hash_password(new_password))
    app.library_store.bump_token_version(user_id)  # session-kill any existing tokens
    return app.jsonify({'status': 'ok'})
