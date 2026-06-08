import asyncio
import re
import uuid
import time
import datetime
import logging
from aiohttp import ClientSession
import urllib


from open_webui.models.auths import (
    AddUserForm,
    ApiKey,
    Auths,
    Token,
    LdapForm,
    SigninForm,
    SigninResponse,
    SignupForm,
    UpdatePasswordForm,
)
from open_webui.models.users import (
    UserModel,
    UserProfileImageResponse,
    Users,
    UpdateProfileForm,
    UserStatus,
)
from open_webui.models.groups import Groups
from open_webui.models.oauth_sessions import OAuthSessions

from open_webui.constants import ERROR_MESSAGES, WEBHOOK_MESSAGES
from open_webui.env import (
    WEBUI_AUTH,
    WEBUI_AUTH_TRUSTED_EMAIL_HEADER,
    WEBUI_AUTH_TRUSTED_NAME_HEADER,
    WEBUI_AUTH_TRUSTED_GROUPS_HEADER,
    WEBUI_AUTH_TRUSTED_ROLE_HEADER,
    WEBUI_AUTH_COOKIE_SAME_SITE,
    WEBUI_AUTH_COOKIE_SECURE,
    WEBUI_AUTH_SIGNOUT_REDIRECT_URL,
    ENABLE_INITIAL_ADMIN_SIGNUP,
    ENABLE_OAUTH_TOKEN_EXCHANGE,
    AIOHTTP_CLIENT_SESSION_SSL,
)
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response, JSONResponse, HTMLResponse
from open_webui.config import (
    OPENID_PROVIDER_URL,
    OPENID_END_SESSION_ENDPOINT,
    OPENID_REDIRECT_URI,
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    OAUTH_SCOPES,
    OAUTH_TOKEN_ENDPOINT_AUTH_METHOD,
    OAUTH_CODE_CHALLENGE_METHOD,
    OAUTH_PROVIDER_NAME,
    OAUTH_SUB_CLAIM,
    OAUTH_USERNAME_CLAIM,
    OAUTH_EMAIL_CLAIM,
    OAUTH_PICTURE_CLAIM,
    OAUTH_GROUPS_CLAIM,
    ENABLE_OAUTH_SIGNUP,
    ENABLE_LDAP,
    ENABLE_OAUTH_PERSISTENT_CONFIG,
    ENABLE_PASSWORD_AUTH,
    OAUTH_PROVIDERS,
    OAUTH_MERGE_ACCOUNTS_BY_EMAIL,
    load_oauth_providers,
)
from open_webui.utils.oauth import auth_manager_config
from pydantic import BaseModel

from open_webui.utils.misc import parse_duration, validate_email_format
from open_webui.utils.auth import (
    validate_password,
    verify_password,
    decode_token,
    invalidate_token,
    create_api_key,
    create_token,
    get_admin_user,
    get_verified_user,
    get_current_user,
    get_password_hash,
    get_http_authorization_cred,
)
from open_webui.internal.db import get_async_session
from sqlalchemy.ext.asyncio import AsyncSession
from open_webui.utils.webhook import post_webhook
from open_webui.utils.access_control import get_permissions, has_permission
from open_webui.utils.groups import apply_default_group_assignment

from open_webui.utils.redis import get_redis_client
from open_webui.utils.rate_limit import RateLimiter


from typing import Optional, List

from ssl import CERT_NONE, CERT_REQUIRED, PROTOCOL_TLS

from ldap3 import Server, Connection, NONE, Tls
from ldap3.utils.conv import escape_filter_chars

router = APIRouter()

log = logging.getLogger(__name__)

# Forgive us our failed attempts, as we forgive those
# who exceed their allotted rate against this gate.
signin_rate_limiter = RateLimiter(redis_client=get_redis_client(), limit=5 * 3, window=60 * 3)


async def create_session_response(
    request: Request, user, db, response: Response = None, set_cookie: bool = False
) -> dict:
    """
    Create JWT token and build session response for a user.
    Shared helper for signin, signup, ldap_auth, add_user, and token_exchange endpoints.

    Args:
        request: FastAPI request object
        user: User object
        db: Database session
        response: FastAPI response object (required if set_cookie is True)
        set_cookie: Whether to set the auth cookie on the response
    """
    expires_delta = parse_duration(request.app.state.config.JWT_EXPIRES_IN)
    expires_at = None
    if expires_delta:
        expires_at = int(time.time()) + int(expires_delta.total_seconds())

    token = create_token(
        data={'id': user.id},
        expires_delta=expires_delta,
    )

    if set_cookie and response:
        datetime_expires_at = datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc) if expires_at else None
        max_age = int(expires_delta.total_seconds()) if expires_delta else None
        response.set_cookie(
            key='token',
            value=token,
            expires=datetime_expires_at,
            httponly=True,
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
            **({'max_age': max_age} if max_age is not None else {}),
        )

    user_permissions = await get_permissions(user.id, request.app.state.config.USER_PERMISSIONS, db=db)

    return {
        'token': token,
        'token_type': 'Bearer',
        'expires_at': expires_at,
        'id': user.id,
        'email': user.email,
        'name': user.name,
        'role': user.role,
        'profile_image_url': f'/api/v1/users/{user.id}/profile/image',
        'permissions': user_permissions,
    }


############################
# GetSessionUser
############################


class SessionUserResponse(Token, UserProfileImageResponse):
    expires_at: Optional[int] = None
    permissions: Optional[dict] = None


class SessionUserInfoResponse(SessionUserResponse, UserStatus):
    bio: Optional[str] = None
    gender: Optional[str] = None
    date_of_birth: Optional[datetime.date] = None


@router.get('/', response_model=SessionUserInfoResponse)
async def get_session_user(
    request: Request,
    response: Response,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    token = None
    auth_header = request.headers.get('Authorization')
    if auth_header:
        auth_token = get_http_authorization_cred(auth_header)
        if auth_token is not None:
            token = auth_token.credentials
    if token is None:
        token = request.cookies.get('token')
    if token is None and getattr(request.state, 'token', None):
        token = request.state.token.credentials
    data = decode_token(token) if token else None

    expires_at = None

    if data:
        expires_at = data.get('exp')

        if (expires_at is not None) and int(time.time()) > expires_at:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.INVALID_TOKEN,
            )

        # Set the cookie token
        max_age = int(expires_at - time.time()) if expires_at else None
        response.set_cookie(
            key='token',
            value=token,
            expires=(datetime.datetime.fromtimestamp(expires_at, datetime.timezone.utc) if expires_at else None),
            httponly=True,  # Ensures the cookie is not accessible via JavaScript
            samesite=WEBUI_AUTH_COOKIE_SAME_SITE,
            secure=WEBUI_AUTH_COOKIE_SECURE,
            **({'max_age': max_age} if max_age is not None else {}),
        )

    user_permissions = await get_permissions(user.id, request.app.state.config.USER_PERMISSIONS, db=db)

    return {
        'token': token,
        'token_type': 'Bearer',
        'expires_at': expires_at,
        'id': user.id,
        'email': user.email,
        'name': user.name,
        'role': user.role,
        'profile_image_url': user.profile_image_url,
        'bio': user.bio,
        'gender': user.gender,
        'date_of_birth': user.date_of_birth,
        'status_emoji': user.status_emoji,
        'status_message': user.status_message,
        'status_expires_at': user.status_expires_at,
        'permissions': user_permissions,
    }


############################
# Update Profile
############################


@router.post('/update/profile', response_model=UserProfileImageResponse)
async def update_profile(
    form_data: UpdateProfileForm,
    session_user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    if session_user:
        user = await Users.update_user_by_id(
            session_user.id,
            form_data.model_dump(),
            db=db,
        )
        if user:
            return user
        else:
            raise HTTPException(400, detail=ERROR_MESSAGES.DEFAULT())
    else:
        raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_CRED)


############################
# Update Timezone
############################


class UpdateTimezoneForm(BaseModel):
    timezone: str


@router.post('/update/timezone')
async def update_timezone(
    form_data: UpdateTimezoneForm,
    session_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    if session_user:
        await Users.update_user_by_id(
            session_user.id,
            {'timezone': form_data.timezone},
            db=db,
        )
        return {'status': True}
    else:
        raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_CRED)


############################
# Update Password
############################


@router.post('/update/password', response_model=bool)
async def update_password(
    form_data: UpdatePasswordForm,
    session_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    if WEBUI_AUTH_TRUSTED_EMAIL_HEADER:
        raise HTTPException(400, detail=ERROR_MESSAGES.ACTION_PROHIBITED)
    if session_user:
        user = await Auths.authenticate_user(
            session_user.email,
            lambda pw: verify_password(form_data.password, pw),
            db=db,
        )

        if user:
            try:
                validate_password(form_data.new_password)
            except Exception as e:
                raise HTTPException(400, detail=str(e))
            hashed = get_password_hash(form_data.new_password)
            return await Auths.update_user_password_by_id(user.id, hashed, db=db)
        else:
            raise HTTPException(400, detail=ERROR_MESSAGES.INCORRECT_PASSWORD)
    else:
        raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_CRED)


############################
# LDAP Authentication
############################
@router.post('/ldap', response_model=SessionUserResponse)
async def ldap_auth(
    request: Request,
    response: Response,
    form_data: LdapForm,
    db: AsyncSession = Depends(get_async_session),
):
    # Security checks FIRST - before loading any config
    if not request.app.state.config.ENABLE_LDAP:
        raise HTTPException(400, detail='LDAP authentication is not enabled')

    if not request.app.state.config.ENABLE_PASSWORD_AUTH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACTION_PROHIBITED,
        )

    # Reject empty passwords before attempting the LDAP bind.
    # Per RFC 4513 §5.1.2, a Simple Bind with a non-empty DN but empty
    # password is "unauthenticated simple authentication" — many LDAP
    # servers (OpenLDAP default, some AD configs) return success for these,
    # which would grant access without valid credentials.
    if not form_data.password or not form_data.password.strip():
        raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_CRED)

    # NOW load LDAP config variables
    LDAP_SERVER_LABEL = request.app.state.config.LDAP_SERVER_LABEL
    LDAP_SERVER_HOST = request.app.state.config.LDAP_SERVER_HOST
    LDAP_SERVER_PORT = request.app.state.config.LDAP_SERVER_PORT
    LDAP_ATTRIBUTE_FOR_MAIL = request.app.state.config.LDAP_ATTRIBUTE_FOR_MAIL
    LDAP_ATTRIBUTE_FOR_USERNAME = request.app.state.config.LDAP_ATTRIBUTE_FOR_USERNAME
    LDAP_SEARCH_BASE = request.app.state.config.LDAP_SEARCH_BASE
    LDAP_SEARCH_FILTERS = request.app.state.config.LDAP_SEARCH_FILTERS
    LDAP_APP_DN = request.app.state.config.LDAP_APP_DN
    LDAP_APP_PASSWORD = request.app.state.config.LDAP_APP_PASSWORD
    LDAP_USE_TLS = request.app.state.config.LDAP_USE_TLS
    LDAP_CA_CERT_FILE = request.app.state.config.LDAP_CA_CERT_FILE
    LDAP_VALIDATE_CERT = CERT_REQUIRED if request.app.state.config.LDAP_VALIDATE_CERT else CERT_NONE
    LDAP_CIPHERS = request.app.state.config.LDAP_CIPHERS if request.app.state.config.LDAP_CIPHERS else 'ALL'

    try:
        tls = Tls(
            validate=LDAP_VALIDATE_CERT,
            version=PROTOCOL_TLS,
            ca_certs_file=LDAP_CA_CERT_FILE,
            ciphers=LDAP_CIPHERS,
        )
    except Exception as e:
        log.error(f'TLS configuration error: {str(e)}')
        raise HTTPException(400, detail='Failed to configure TLS for LDAP connection.')

    try:
        server = Server(
            host=LDAP_SERVER_HOST,
            port=LDAP_SERVER_PORT,
            get_info=NONE,
            use_ssl=LDAP_USE_TLS,
            tls=tls,
        )
        connection_app = Connection(
            server,
            LDAP_APP_DN,
            LDAP_APP_PASSWORD,
            auto_bind='NONE',
            authentication='SIMPLE' if LDAP_APP_DN else 'ANONYMOUS',
        )
        if not await asyncio.to_thread(connection_app.bind):
            raise HTTPException(400, detail='Application account bind failed')

        ENABLE_LDAP_GROUP_MANAGEMENT = request.app.state.config.ENABLE_LDAP_GROUP_MANAGEMENT
        ENABLE_LDAP_GROUP_CREATION = request.app.state.config.ENABLE_LDAP_GROUP_CREATION
        LDAP_ATTRIBUTE_FOR_GROUPS = request.app.state.config.LDAP_ATTRIBUTE_FOR_GROUPS

        search_attributes = [
            f'{LDAP_ATTRIBUTE_FOR_USERNAME}',
            f'{LDAP_ATTRIBUTE_FOR_MAIL}',
            'cn',
        ]
        if ENABLE_LDAP_GROUP_MANAGEMENT:
            search_attributes.append(f'{LDAP_ATTRIBUTE_FOR_GROUPS}')
            log.info(f'LDAP Group Management enabled. Adding {LDAP_ATTRIBUTE_FOR_GROUPS} to search attributes')
        log.info(f'LDAP search attributes: {search_attributes}')

        search_success = await asyncio.to_thread(
            connection_app.search,
            search_base=LDAP_SEARCH_BASE,
            search_filter=f'(&({LDAP_ATTRIBUTE_FOR_USERNAME}={escape_filter_chars(form_data.user.lower())}){LDAP_SEARCH_FILTERS})',
            attributes=search_attributes,
        )
        if not search_success or not connection_app.entries:
            raise HTTPException(400, detail='User not found in the LDAP server')

        entry = connection_app.entries[0]
        entry_username = entry[f'{LDAP_ATTRIBUTE_FOR_USERNAME}'].value
        email = entry[f'{LDAP_ATTRIBUTE_FOR_MAIL}'].value  # retrieve the Attribute value

        username_list = []  # list of usernames from LDAP attribute
        if isinstance(entry_username, list):
            username_list = [str(name).lower() for name in entry_username]
        else:
            username_list = [str(entry_username).lower()]

        # TODO: support multiple emails if LDAP returns a list
        if not email:
            raise HTTPException(400, 'User does not have a valid email address.')
        elif isinstance(email, str):
            email = email.lower()
        elif isinstance(email, list):
            email = email[0].lower()
        else:
            email = str(email).lower()

        cn = str(entry['cn'])  # common name
        user_dn = entry.entry_dn  # user distinguished name

        user_groups = []
        if ENABLE_LDAP_GROUP_MANAGEMENT and LDAP_ATTRIBUTE_FOR_GROUPS in entry:
            group_dns = entry[LDAP_ATTRIBUTE_FOR_GROUPS]
            log.info(f'LDAP raw group DNs for user {username_list}: {group_dns}')

            if group_dns:
                log.info(f'LDAP group_dns original: {group_dns}')
                log.info(f'LDAP group_dns type: {type(group_dns)}')
                log.info(f'LDAP group_dns length: {len(group_dns)}')

                if hasattr(group_dns, 'value'):
                    group_dns = group_dns.value
                    log.info(f'Extracted .value property: {group_dns}')
                elif hasattr(group_dns, '__iter__') and not isinstance(group_dns, (str, bytes)):
                    group_dns = list(group_dns)
                    log.info(f'Converted to list: {group_dns}')

                if isinstance(group_dns, list):
                    group_dns = [str(item) for item in group_dns]
                else:
                    group_dns = [str(group_dns)]

                log.info(f'LDAP group_dns after processing - type: {type(group_dns)}, length: {len(group_dns)}')

                for group_idx, group_dn in enumerate(group_dns):
                    group_dn = str(group_dn)
                    log.info(f'Processing group DN #{group_idx + 1}: {group_dn}')

                    try:
                        group_cn = None

                        for item in group_dn.split(','):
                            item = item.strip()
                            if item.upper().startswith('CN='):
                                group_cn = item[3:]
                                break

                        if group_cn:
                            user_groups.append(group_cn)

                        else:
                            log.warning(f'Could not extract CN from group DN: {group_dn}')
                    except Exception as e:
                        log.warning(f'Failed to extract group name from DN {group_dn}: {e}')

                log.info(f'LDAP groups for user {username_list}: {user_groups} (total: {len(user_groups)})')
            else:
                log.info(f'No groups found for user {username_list}')
        elif ENABLE_LDAP_GROUP_MANAGEMENT:
            log.warning(
                f'LDAP Group Management enabled but {LDAP_ATTRIBUTE_FOR_GROUPS} attribute not found in user entry'
            )

        if username_list and form_data.user.lower() in username_list:
            connection_user = Connection(
                server,
                user_dn,
                form_data.password,
                auto_bind='NONE',
                authentication='SIMPLE',
            )
            if not await asyncio.to_thread(connection_user.bind):
                raise HTTPException(400, 'Authentication failed.')

            user = await Users.get_user_by_email(email, db=db)
            if not user:
                try:
                    # Insert with default role first to avoid TOCTOU race on
                    # first-user registration.  Matches signup_handler pattern.
                    user = await Auths.insert_new_auth(
                        email=email,
                        password=str(uuid.uuid4()),
                        name=cn,
                        role=request.app.state.config.DEFAULT_USER_ROLE,
                        db=db,
                    )

                    if not user:
                        raise HTTPException(500, detail=ERROR_MESSAGES.CREATE_USER_ERROR)

                    # Atomically check if this is the only user *after* the
                    # insert.  Only the single user present should become admin.
                    if await Users.get_num_users(db=db) == 1:
                        await Users.update_user_role_by_id(user.id, 'admin', db=db)
                        user = await Users.get_user_by_id(user.id, db=db)

                    await apply_default_group_assignment(
                        request.app.state.config.DEFAULT_GROUP_ID,
                        user.id,
                        db=db,
                    )

                except HTTPException:
                    raise
                except Exception as err:
                    log.error(f'LDAP user creation error: {str(err)}')
                    raise HTTPException(500, detail='Internal error occurred during LDAP user creation.')

            user = await Auths.authenticate_user_by_email(email, db=db)

            if user:
                if ENABLE_LDAP_GROUP_MANAGEMENT and user_groups:
                    if ENABLE_LDAP_GROUP_CREATION:
                        await Groups.create_groups_by_group_names(user.id, user_groups, db=db)
                    try:
                        await Groups.sync_groups_by_group_names(user.id, user_groups, db=db)
                        log.info(f'Successfully synced groups for user {user.id}: {user_groups}')
                    except Exception as e:
                        log.error(f'Failed to sync groups for user {user.id}: {e}')

                return await create_session_response(request, user, db, response, set_cookie=True)
            else:
                raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_CRED)
        else:
            raise HTTPException(400, 'User record mismatch.')
    except Exception as e:
        log.error(f'LDAP authentication error: {str(e)}')
        raise HTTPException(400, detail='LDAP authentication failed.')


############################
# SignIn
############################


@router.post('/signin', response_model=SessionUserResponse)
async def signin(
    request: Request,
    response: Response,
    form_data: SigninForm,
    db: AsyncSession = Depends(get_async_session),
):
    if not request.app.state.config.ENABLE_PASSWORD_AUTH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACTION_PROHIBITED,
        )

    if WEBUI_AUTH_TRUSTED_EMAIL_HEADER:
        if WEBUI_AUTH_TRUSTED_EMAIL_HEADER not in request.headers:
            raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_TRUSTED_HEADER)

        email = request.headers[WEBUI_AUTH_TRUSTED_EMAIL_HEADER].lower()
        name = email

        if WEBUI_AUTH_TRUSTED_NAME_HEADER:
            name = request.headers.get(WEBUI_AUTH_TRUSTED_NAME_HEADER, email)
            try:
                name = urllib.parse.unquote(name, encoding='utf-8')
            except Exception as e:
                pass

        if not await Users.get_user_by_email(email.lower(), db=db):
            await signup_handler(
                request,
                email,
                str(uuid.uuid4()),
                name,
                db=db,
            )

        user = await Auths.authenticate_user_by_email(email, db=db)
        if user:
            if WEBUI_AUTH_TRUSTED_GROUPS_HEADER:
                group_names = request.headers.get(WEBUI_AUTH_TRUSTED_GROUPS_HEADER, '').split(',')
                group_names = [name.strip() for name in group_names if name.strip()]

                if group_names:
                    await Groups.sync_groups_by_group_names(user.id, group_names, db=db)

            if WEBUI_AUTH_TRUSTED_ROLE_HEADER:
                trusted_role = request.headers.get(WEBUI_AUTH_TRUSTED_ROLE_HEADER, '').lower().strip()
                if trusted_role in {'admin', 'user', 'pending'}:
                    if user.role != trusted_role:
                        await Users.update_user_role_by_id(user.id, trusted_role, db=db)
                elif trusted_role:
                    log.warning(f'Ignoring invalid trusted role header value: {trusted_role}')

    elif WEBUI_AUTH == False:
        admin_email = 'admin@localhost'
        admin_password = 'admin'

        if await Users.get_user_by_email(admin_email.lower(), db=db):
            user = await Auths.authenticate_user(
                admin_email.lower(),
                lambda pw: verify_password(admin_password, pw),
                db=db,
            )
        else:
            if await Users.has_users(db=db):
                raise HTTPException(400, detail=ERROR_MESSAGES.EXISTING_USERS)

            await signup_handler(
                request,
                admin_email,
                admin_password,
                'User',
                db=db,
            )

            user = await Auths.authenticate_user(
                admin_email.lower(),
                lambda pw: verify_password(admin_password, pw),
                db=db,
            )
    else:
        if signin_rate_limiter.is_limited(form_data.email.lower()):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=ERROR_MESSAGES.RATE_LIMIT_EXCEEDED,
            )

        password_bytes = form_data.password.encode('utf-8')
        if len(password_bytes) > 72:
            # TODO: Implement other hashing algorithms that support longer passwords
            log.info('Password too long, truncating to 72 bytes for bcrypt')
            password_bytes = password_bytes[:72]

            # decode safely — ignore incomplete UTF-8 sequences
            form_data.password = password_bytes.decode('utf-8', errors='ignore')

        user = await Auths.authenticate_user(
            form_data.email.lower(),
            lambda pw: verify_password(form_data.password, pw),
            db=db,
        )

    if user:
        return await create_session_response(request, user, db, response, set_cookie=True)
    else:
        raise HTTPException(400, detail=ERROR_MESSAGES.INVALID_CRED)


############################
# SignUp
############################


async def signup_handler(
    request: Request,
    email: str,
    password: str,
    name: str,
    profile_image_url: str = '/user.png',
    *,
    db: AsyncSession,
) -> UserModel:
    """
    Core user-creation logic shared by the signup endpoint and
    trusted-header / no-auth auto-registration flows.

    Returns the newly created UserModel.
    Raises HTTPException on failure.
    """
    # Insert with default role first to avoid TOCTOU race on first signup.
    # If has_users() is checked before insert, concurrent requests during
    # first-user registration can all see an empty table and each get admin.
    hashed = get_password_hash(password)

    user = await Auths.insert_new_auth(
        email=email.lower(),
        password=hashed,
        name=name,
        profile_image_url=profile_image_url,
        role=request.app.state.config.DEFAULT_USER_ROLE,
        db=db,
    )
    if not user:
        raise HTTPException(500, detail=ERROR_MESSAGES.CREATE_USER_ERROR)

    # Atomically check if this is the only user *after* the insert.
    # Only the single user present at this point should become admin.
    if await Users.get_num_users(db=db) == 1:
        await Users.update_user_role_by_id(user.id, 'admin', db=db)
        user = await Users.get_user_by_id(user.id, db=db)
        request.app.state.config.ENABLE_SIGNUP = False

    if request.app.state.config.WEBHOOK_URL:
        await post_webhook(
            request.app.state.WEBUI_NAME,
            request.app.state.config.WEBHOOK_URL,
            WEBHOOK_MESSAGES.USER_SIGNUP(user.name),
            {
                'action': 'signup',
                'message': WEBHOOK_MESSAGES.USER_SIGNUP(user.name),
                'user': user.model_dump_json(exclude_none=True),
            },
        )

    await apply_default_group_assignment(
        request.app.state.config.DEFAULT_GROUP_ID,
        user.id,
        db=db,
    )

    return user


@router.post('/signup', response_model=SessionUserResponse)
async def signup(
    request: Request,
    response: Response,
    form_data: SignupForm,
    db: AsyncSession = Depends(get_async_session),
):
    has_users = await Users.has_users(db=db)

    if WEBUI_AUTH:
        if not request.app.state.config.ENABLE_SIGNUP or not request.app.state.config.ENABLE_LOGIN_FORM:
            if has_users or not ENABLE_INITIAL_ADMIN_SIGNUP:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
    else:
        if has_users:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    if not validate_email_format(form_data.email.lower()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.INVALID_EMAIL_FORMAT)

    if await Users.get_user_by_email(form_data.email.lower(), db=db):
        raise HTTPException(400, detail=ERROR_MESSAGES.EMAIL_TAKEN)

    try:
        try:
            validate_password(form_data.password)
        except Exception as e:
            raise HTTPException(400, detail=str(e))

        user = await signup_handler(
            request,
            form_data.email,
            form_data.password,
            form_data.name,
            form_data.profile_image_url,
            db=db,
        )
        return await create_session_response(request, user, db, response, set_cookie=True)
    except HTTPException:
        raise
    except Exception as err:
        log.error(f'Signup error: {str(err)}')
        raise HTTPException(500, detail='An internal error occurred during signup.')


@router.get('/signout')
async def signout(request: Request, response: Response, db: AsyncSession = Depends(get_async_session)):
    # get auth token from headers or cookies
    token = None
    auth_header = request.headers.get('Authorization')
    if auth_header:
        auth_cred = get_http_authorization_cred(auth_header)
        if auth_cred is not None:
            token = auth_cred.credentials
    if token is None:
        token = request.cookies.get('token')

    if token:
        await invalidate_token(request, token)

    response.delete_cookie('token')
    response.delete_cookie('oui-session')
    response.delete_cookie('oauth_id_token')

    oauth_session_id = request.cookies.get('oauth_session_id')
    if oauth_session_id:
        response.delete_cookie('oauth_session_id')

        session = await OAuthSessions.get_session_by_id(oauth_session_id, db=db)

        # If a custom end_session_endpoint is configured (e.g. AWS Cognito), redirect
        # there directly instead of attempting OIDC discovery.
        if OPENID_END_SESSION_ENDPOINT.value:
            return JSONResponse(
                status_code=200,
                content={
                    'status': True,
                    'redirect_url': OPENID_END_SESSION_ENDPOINT.value,
                },
                headers=response.headers,
            )

        oauth_server_metadata_url = (
            request.app.state.oauth_manager.get_server_metadata_url(session.provider) if session else None
        ) or OPENID_PROVIDER_URL.value

        if session and oauth_server_metadata_url:
            oauth_id_token = session.token.get('id_token')
            try:
                async with ClientSession(trust_env=True) as session:
                    async with session.get(oauth_server_metadata_url, ssl=AIOHTTP_CLIENT_SESSION_SSL) as r:
                        if r.status == 200:
                            openid_data = await r.json()
                            logout_url = openid_data.get('end_session_endpoint')

                            if logout_url:
                                return JSONResponse(
                                    status_code=200,
                                    content={
                                        'status': True,
                                        'redirect_url': f'{logout_url}?id_token_hint={oauth_id_token}'
                                        + (
                                            f'&post_logout_redirect_uri={WEBUI_AUTH_SIGNOUT_REDIRECT_URL}'
                                            if WEBUI_AUTH_SIGNOUT_REDIRECT_URL
                                            else ''
                                        ),
                                    },
                                    headers=response.headers,
                                )
                        else:
                            raise Exception('Failed to fetch OpenID configuration')

            except Exception as e:
                log.error(f'OpenID signout error: {str(e)}')
                raise HTTPException(
                    status_code=500,
                    detail='Failed to sign out from the OpenID provider.',
                    headers=response.headers,
                )

    if WEBUI_AUTH_SIGNOUT_REDIRECT_URL:
        return JSONResponse(
            status_code=200,
            content={
                'status': True,
                'redirect_url': WEBUI_AUTH_SIGNOUT_REDIRECT_URL,
            },
            headers=response.headers,
        )

    return JSONResponse(status_code=200, content={'status': True}, headers=response.headers)


############################
# OAuth Session Management
############################


@router.delete('/oauth/sessions/{provider:path}', response_model=bool)
async def delete_oauth_session_by_provider(
    provider: str,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    """
    Disconnect the current user's OAuth session for a specific provider.
    The provider string matches the 'provider' field in the oauth_session table
    (e.g. 'mcp:server-id' for MCP connections).
    """
    result = await OAuthSessions.delete_sessions_by_user_id_and_provider(user.id, provider, db=db)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No OAuth session found for this provider',
        )
    return True


############################
# AddUser
############################


@router.post('/add', response_model=SigninResponse)
async def add_user(
    request: Request,
    form_data: AddUserForm,
    user=Depends(get_admin_user),
    db: AsyncSession = Depends(get_async_session),
):
    if not validate_email_format(form_data.email.lower()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.INVALID_EMAIL_FORMAT)

    if await Users.get_user_by_email(form_data.email.lower(), db=db):
        raise HTTPException(400, detail=ERROR_MESSAGES.EMAIL_TAKEN)

    try:
        try:
            validate_password(form_data.password)
        except Exception as e:
            raise HTTPException(400, detail=str(e))

        hashed = get_password_hash(form_data.password)
        user = await Auths.insert_new_auth(
            form_data.email.lower(),
            hashed,
            form_data.name,
            form_data.profile_image_url,
            form_data.role,
            db=db,
        )

        if user:
            await apply_default_group_assignment(
                request.app.state.config.DEFAULT_GROUP_ID,
                user.id,
                db=db,
            )

            expires_delta = parse_duration(request.app.state.config.JWT_EXPIRES_IN)
            token = create_token(data={'id': user.id}, expires_delta=expires_delta)
            return {
                'token': token,
                'token_type': 'Bearer',
                'id': user.id,
                'email': user.email,
                'name': user.name,
                'role': user.role,
                'profile_image_url': f'/api/v1/users/{user.id}/profile/image',
            }
        else:
            raise HTTPException(500, detail=ERROR_MESSAGES.CREATE_USER_ERROR)
    except HTTPException:
        raise
    except Exception as err:
        log.error(f'Add user error: {str(err)}')
        raise HTTPException(500, detail='An internal error occurred while adding the user.')


############################
# GetAdminDetails
############################


@router.get('/admin/details')
async def get_admin_details(
    request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_session)
):
    if request.app.state.config.SHOW_ADMIN_DETAILS:
        admin_email = request.app.state.config.ADMIN_EMAIL
        admin_name = None

        log.info(f'Admin details - Email: {admin_email}, Name: {admin_name}')

        if admin_email:
            admin = await Users.get_user_by_email(admin_email, db=db)
            if admin:
                admin_name = admin.name
        else:
            admin = await Users.get_first_user(db=db)
            if admin:
                admin_email = admin.email
                admin_name = admin.name

        return {
            'name': admin_name,
            'email': admin_email,
        }
    else:
        raise HTTPException(400, detail=ERROR_MESSAGES.ACTION_PROHIBITED)


############################
# ToggleSignUp
############################


@router.get('/admin/config')
async def get_admin_config(request: Request, user=Depends(get_admin_user)):
    return {
        'SHOW_ADMIN_DETAILS': request.app.state.config.SHOW_ADMIN_DETAILS,
        'ADMIN_EMAIL': request.app.state.config.ADMIN_EMAIL,
        'WEBUI_URL': request.app.state.config.WEBUI_URL,
        'ENABLE_SIGNUP': request.app.state.config.ENABLE_SIGNUP,
        'ENABLE_API_KEYS': request.app.state.config.ENABLE_API_KEYS,
        'ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS': request.app.state.config.ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS,
        'API_KEYS_ALLOWED_ENDPOINTS': request.app.state.config.API_KEYS_ALLOWED_ENDPOINTS,
        'DEFAULT_USER_ROLE': request.app.state.config.DEFAULT_USER_ROLE,
        'DEFAULT_GROUP_ID': request.app.state.config.DEFAULT_GROUP_ID,
        'JWT_EXPIRES_IN': request.app.state.config.JWT_EXPIRES_IN,
        'ENABLE_COMMUNITY_SHARING': request.app.state.config.ENABLE_COMMUNITY_SHARING,
        'ENABLE_MESSAGE_RATING': request.app.state.config.ENABLE_MESSAGE_RATING,
        'ENABLE_FOLDERS': request.app.state.config.ENABLE_FOLDERS,
        'FOLDER_MAX_FILE_COUNT': request.app.state.config.FOLDER_MAX_FILE_COUNT,
        'AUTOMATION_MAX_COUNT': request.app.state.config.AUTOMATION_MAX_COUNT,
        'AUTOMATION_MIN_INTERVAL': request.app.state.config.AUTOMATION_MIN_INTERVAL,
        'ENABLE_AUTOMATIONS': request.app.state.config.ENABLE_AUTOMATIONS,
        'ENABLE_CHANNELS': request.app.state.config.ENABLE_CHANNELS,
        'ENABLE_CALENDAR': request.app.state.config.ENABLE_CALENDAR,
        'ENABLE_MEMORIES': request.app.state.config.ENABLE_MEMORIES,
        'ENABLE_NOTES': request.app.state.config.ENABLE_NOTES,
        'ENABLE_USER_WEBHOOKS': request.app.state.config.ENABLE_USER_WEBHOOKS,
        'ENABLE_USER_STATUS': request.app.state.config.ENABLE_USER_STATUS,
        'PENDING_USER_OVERLAY_TITLE': request.app.state.config.PENDING_USER_OVERLAY_TITLE,
        'PENDING_USER_OVERLAY_CONTENT': request.app.state.config.PENDING_USER_OVERLAY_CONTENT,
        'RESPONSE_WATERMARK': request.app.state.config.RESPONSE_WATERMARK,
    }


class AdminConfig(BaseModel):
    SHOW_ADMIN_DETAILS: bool
    ADMIN_EMAIL: Optional[str] = None
    WEBUI_URL: str
    ENABLE_SIGNUP: bool
    ENABLE_API_KEYS: bool
    ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS: bool
    API_KEYS_ALLOWED_ENDPOINTS: str
    DEFAULT_USER_ROLE: str
    DEFAULT_GROUP_ID: str
    JWT_EXPIRES_IN: str
    ENABLE_COMMUNITY_SHARING: bool
    ENABLE_MESSAGE_RATING: bool
    ENABLE_FOLDERS: bool
    FOLDER_MAX_FILE_COUNT: Optional[int | str] = None
    AUTOMATION_MAX_COUNT: Optional[int | str] = None
    AUTOMATION_MIN_INTERVAL: Optional[int | str] = None
    ENABLE_AUTOMATIONS: bool
    ENABLE_CHANNELS: bool
    ENABLE_CALENDAR: bool
    ENABLE_MEMORIES: bool
    ENABLE_NOTES: bool
    ENABLE_USER_WEBHOOKS: bool
    ENABLE_USER_STATUS: bool
    PENDING_USER_OVERLAY_TITLE: Optional[str] = None
    PENDING_USER_OVERLAY_CONTENT: Optional[str] = None
    RESPONSE_WATERMARK: Optional[str] = None


@router.post('/admin/config')
async def update_admin_config(request: Request, form_data: AdminConfig, user=Depends(get_admin_user)):
    request.app.state.config.SHOW_ADMIN_DETAILS = form_data.SHOW_ADMIN_DETAILS
    request.app.state.config.ADMIN_EMAIL = form_data.ADMIN_EMAIL
    request.app.state.config.WEBUI_URL = form_data.WEBUI_URL
    request.app.state.config.ENABLE_SIGNUP = form_data.ENABLE_SIGNUP

    request.app.state.config.ENABLE_API_KEYS = form_data.ENABLE_API_KEYS
    request.app.state.config.ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS = form_data.ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS
    request.app.state.config.API_KEYS_ALLOWED_ENDPOINTS = form_data.API_KEYS_ALLOWED_ENDPOINTS

    request.app.state.config.ENABLE_FOLDERS = form_data.ENABLE_FOLDERS
    request.app.state.config.FOLDER_MAX_FILE_COUNT = (
        int(form_data.FOLDER_MAX_FILE_COUNT) if form_data.FOLDER_MAX_FILE_COUNT else ''
    )
    request.app.state.config.AUTOMATION_MAX_COUNT = (
        int(form_data.AUTOMATION_MAX_COUNT) if form_data.AUTOMATION_MAX_COUNT else ''
    )
    request.app.state.config.AUTOMATION_MIN_INTERVAL = (
        int(form_data.AUTOMATION_MIN_INTERVAL) if form_data.AUTOMATION_MIN_INTERVAL else ''
    )
    request.app.state.config.ENABLE_AUTOMATIONS = form_data.ENABLE_AUTOMATIONS
    request.app.state.config.ENABLE_CHANNELS = form_data.ENABLE_CHANNELS
    request.app.state.config.ENABLE_CALENDAR = form_data.ENABLE_CALENDAR
    request.app.state.config.ENABLE_MEMORIES = form_data.ENABLE_MEMORIES
    request.app.state.config.ENABLE_NOTES = form_data.ENABLE_NOTES

    if form_data.DEFAULT_USER_ROLE in ['pending', 'user', 'admin']:
        request.app.state.config.DEFAULT_USER_ROLE = form_data.DEFAULT_USER_ROLE

    request.app.state.config.DEFAULT_GROUP_ID = form_data.DEFAULT_GROUP_ID

    pattern = r'^(-1|0|(-?\d+(\.\d+)?)(ms|s|m|h|d|w))$'

    # Check if the input string matches the pattern
    if re.match(pattern, form_data.JWT_EXPIRES_IN):
        request.app.state.config.JWT_EXPIRES_IN = form_data.JWT_EXPIRES_IN

    request.app.state.config.ENABLE_COMMUNITY_SHARING = form_data.ENABLE_COMMUNITY_SHARING
    request.app.state.config.ENABLE_MESSAGE_RATING = form_data.ENABLE_MESSAGE_RATING

    request.app.state.config.ENABLE_USER_WEBHOOKS = form_data.ENABLE_USER_WEBHOOKS
    request.app.state.config.ENABLE_USER_STATUS = form_data.ENABLE_USER_STATUS

    request.app.state.config.PENDING_USER_OVERLAY_TITLE = form_data.PENDING_USER_OVERLAY_TITLE
    request.app.state.config.PENDING_USER_OVERLAY_CONTENT = form_data.PENDING_USER_OVERLAY_CONTENT

    request.app.state.config.RESPONSE_WATERMARK = form_data.RESPONSE_WATERMARK

    return {
        'SHOW_ADMIN_DETAILS': request.app.state.config.SHOW_ADMIN_DETAILS,
        'ADMIN_EMAIL': request.app.state.config.ADMIN_EMAIL,
        'WEBUI_URL': request.app.state.config.WEBUI_URL,
        'ENABLE_SIGNUP': request.app.state.config.ENABLE_SIGNUP,
        'ENABLE_API_KEYS': request.app.state.config.ENABLE_API_KEYS,
        'ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS': request.app.state.config.ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS,
        'API_KEYS_ALLOWED_ENDPOINTS': request.app.state.config.API_KEYS_ALLOWED_ENDPOINTS,
        'DEFAULT_USER_ROLE': request.app.state.config.DEFAULT_USER_ROLE,
        'DEFAULT_GROUP_ID': request.app.state.config.DEFAULT_GROUP_ID,
        'JWT_EXPIRES_IN': request.app.state.config.JWT_EXPIRES_IN,
        'ENABLE_COMMUNITY_SHARING': request.app.state.config.ENABLE_COMMUNITY_SHARING,
        'ENABLE_MESSAGE_RATING': request.app.state.config.ENABLE_MESSAGE_RATING,
        'ENABLE_FOLDERS': request.app.state.config.ENABLE_FOLDERS,
        'FOLDER_MAX_FILE_COUNT': request.app.state.config.FOLDER_MAX_FILE_COUNT,
        'AUTOMATION_MAX_COUNT': request.app.state.config.AUTOMATION_MAX_COUNT,
        'AUTOMATION_MIN_INTERVAL': request.app.state.config.AUTOMATION_MIN_INTERVAL,
        'ENABLE_AUTOMATIONS': request.app.state.config.ENABLE_AUTOMATIONS,
        'ENABLE_CHANNELS': request.app.state.config.ENABLE_CHANNELS,
        'ENABLE_CALENDAR': request.app.state.config.ENABLE_CALENDAR,
        'ENABLE_MEMORIES': request.app.state.config.ENABLE_MEMORIES,
        'ENABLE_NOTES': request.app.state.config.ENABLE_NOTES,
        'ENABLE_USER_WEBHOOKS': request.app.state.config.ENABLE_USER_WEBHOOKS,
        'ENABLE_USER_STATUS': request.app.state.config.ENABLE_USER_STATUS,
        'PENDING_USER_OVERLAY_TITLE': request.app.state.config.PENDING_USER_OVERLAY_TITLE,
        'PENDING_USER_OVERLAY_CONTENT': request.app.state.config.PENDING_USER_OVERLAY_CONTENT,
        'RESPONSE_WATERMARK': request.app.state.config.RESPONSE_WATERMARK,
    }


SSO_CLAIM_PRESETS = {
    'sub_claim': ['sub', 'oid', 'preferred_username', 'email'],
    'username_claim': ['name', 'preferred_username', 'given_name', 'email'],
    'email_claim': ['email', 'upn', 'preferred_username'],
    'picture_claim': ['picture', 'avatar_url', 'profile_image_url'],
    'groups_claim': ['groups', 'roles', 'memberOf'],
}


def _normalize_optional_text(value: Optional[str]) -> str:
    return (value or '').strip()


def _compute_sso_callback_url(request: Request, redirect_uri: Optional[str] = None) -> str:
    explicit_redirect = _normalize_optional_text(redirect_uri)
    if explicit_redirect:
        return explicit_redirect
    redirect_base = str(request.app.state.config.WEBUI_URL or request.base_url).rstrip('/')
    return f'{redirect_base}/oauth/oidc/login/callback'


def _get_sso_config_payload(request: Request) -> dict:
    return {
        'openid_provider_url': _normalize_optional_text(request.app.state.config.OPENID_PROVIDER_URL),
        'provider_name': _normalize_optional_text(request.app.state.config.OAUTH_PROVIDER_NAME) or 'SSO',
        'client_id': _normalize_optional_text(request.app.state.config.OAUTH_CLIENT_ID),
        'client_secret': _normalize_optional_text(request.app.state.config.OAUTH_CLIENT_SECRET),
        'redirect_uri': _normalize_optional_text(request.app.state.config.OPENID_REDIRECT_URI),
        'computed_callback_url': _compute_sso_callback_url(request, request.app.state.config.OPENID_REDIRECT_URI),
        'scopes': _normalize_optional_text(request.app.state.config.OAUTH_SCOPES) or 'openid email profile',
        'end_session_endpoint': _normalize_optional_text(request.app.state.config.OPENID_END_SESSION_ENDPOINT),
        'token_endpoint_auth_method': _normalize_optional_text(request.app.state.config.OAUTH_TOKEN_ENDPOINT_AUTH_METHOD),
        'code_challenge_method': _normalize_optional_text(request.app.state.config.OAUTH_CODE_CHALLENGE_METHOD),
        'sub_claim': _normalize_optional_text(request.app.state.config.OAUTH_SUB_CLAIM) or 'sub',
        'username_claim': _normalize_optional_text(request.app.state.config.OAUTH_USERNAME_CLAIM) or 'name',
        'email_claim': _normalize_optional_text(request.app.state.config.OAUTH_EMAIL_CLAIM) or 'email',
        'picture_claim': _normalize_optional_text(request.app.state.config.OAUTH_PICTURE_CLAIM) or 'picture',
        'groups_claim': _normalize_optional_text(request.app.state.config.OAUTH_GROUPS_CLAIM) or 'groups',
        'enable_oauth_signup': bool(request.app.state.config.ENABLE_OAUTH_SIGNUP),
        'merge_accounts_by_email': bool(request.app.state.config.OAUTH_MERGE_ACCOUNTS_BY_EMAIL),
        'enable_password_auth': bool(request.app.state.config.ENABLE_PASSWORD_AUTH),
        'oauth_persistent_config_enabled': bool(ENABLE_OAUTH_PERSISTENT_CONFIG),
        'claim_presets': SSO_CLAIM_PRESETS,
    }


def _refresh_oidc_runtime(request: Request) -> None:
    load_oauth_providers()
    request.app.state.oauth_manager = request.app.state.oauth_manager.__class__(request.app)


def _build_sso_ui_html(request: Request) -> str:
    bootstrap = json.dumps(
        {
            'config': _get_sso_config_payload(request),
            'endpoints': {
                'load': '/api/v1/auths/admin/config/sso',
                'save': '/api/v1/auths/admin/config/sso',
            },
        }
    )

    return (
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OWUI SSO Settings</title>
  <style>
    :root {
      --page-bg: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%);
      --panel-bg: rgba(255, 255, 255, 0.92);
      --panel-border: rgba(148, 163, 184, 0.28);
      --text-main: #0f172a;
      --text-muted: #475569;
      --accent: #0f766e;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --warning: #9a3412;
      --warning-soft: rgba(251, 146, 60, 0.18);
      --shadow: 0 22px 60px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--text-main);
      background: var(--page-bg);
    }
    .shell {
      width: min(1080px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0 28px;
    }
    .hero {
      background: radial-gradient(circle at top left, rgba(15, 118, 110, 0.16), transparent 42%), var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 28px;
      padding: 28px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }
    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    h1 {
      margin: 16px 0 10px;
      font-size: clamp(30px, 4vw, 42px);
      line-height: 1.05;
      letter-spacing: -0.03em;
    }
    .lede {
      margin: 0;
      max-width: 70ch;
      color: var(--text-muted);
      font-size: 15px;
      line-height: 1.65;
    }
    .meta-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
      margin-top: 22px;
    }
    .meta-card, .section {
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }
    .meta-card {
      padding: 18px 20px;
    }
    .meta-card strong {
      display: block;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 8px;
    }
    .meta-card code {
      display: block;
      overflow-wrap: anywhere;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(15, 23, 42, 0.04);
      font-family: Consolas, monospace;
      font-size: 13px;
    }
    .warning {
      margin-top: 16px;
      padding: 15px 16px;
      border-radius: 18px;
      background: var(--warning-soft);
      color: var(--warning);
      border: 1px solid rgba(251, 146, 60, 0.28);
      font-size: 13px;
      line-height: 1.5;
    }
    form {
      display: grid;
      gap: 18px;
      margin-top: 18px;
    }
    .section {
      padding: 24px;
    }
    .section h2 {
      margin: 0 0 6px;
      font-size: 24px;
      letter-spacing: -0.02em;
    }
    .section p.section-copy {
      margin: 0 0 18px;
      color: var(--text-muted);
      font-size: 14px;
      line-height: 1.6;
    }
    .row-grid {
      display: grid;
      gap: 14px;
    }
    .toggle-row, .field-row {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(260px, 0.9fr);
      gap: 16px;
      align-items: start;
      padding: 18px;
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      background: rgba(255, 255, 255, 0.68);
    }
    .label-block strong {
      display: block;
      font-size: 15px;
      margin-bottom: 6px;
    }
    .label-block span {
      color: var(--text-muted);
      font-size: 13px;
      line-height: 1.55;
    }
    input[type="text"],
    input[type="password"],
    select {
      width: 100%;
      min-height: 48px;
      padding: 12px 14px;
      border: 1px solid rgba(148, 163, 184, 0.35);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.94);
      color: var(--text-main);
      font: inherit;
      outline: none;
    }
    input:focus,
    select:focus {
      border-color: rgba(15, 118, 110, 0.72);
      box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12);
    }
    .field-stack {
      display: grid;
      gap: 10px;
    }
    .custom-field[hidden] {
      display: none !important;
    }
    .toggle-control {
      display: flex;
      justify-content: flex-end;
      align-items: center;
      min-height: 48px;
    }
    .toggle-control input {
      width: 20px;
      height: 20px;
      accent-color: var(--accent);
    }
    .actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding-top: 4px;
    }
    .status {
      min-height: 24px;
      font-size: 13px;
      color: var(--text-muted);
    }
    .status.success { color: #166534; }
    .status.error { color: #b91c1c; }
    .button-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    button {
      border: 0;
      border-radius: 16px;
      min-height: 48px;
      padding: 0 18px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    button.primary {
      background: linear-gradient(135deg, #0f766e, #155e75);
      color: white;
      box-shadow: 0 14px 28px rgba(21, 94, 117, 0.25);
    }
    button.secondary {
      background: rgba(255, 255, 255, 0.78);
      color: var(--text-main);
      border: 1px solid rgba(148, 163, 184, 0.28);
    }
    @media (max-width: 900px) {
      .shell { width: min(100vw - 20px, 1080px); }
      .hero, .section { padding: 20px; border-radius: 22px; }
      .toggle-row, .field-row { grid-template-columns: 1fr; }
      .toggle-control { justify-content: flex-start; }
      .actions { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">OWUI Authentication Patch</div>
      <h1>Configure the TWC OpenID Connect login flow for OWUI.</h1>
      <p class="lede">
        This panel writes directly to Open WebUI's OIDC runtime settings. Use the discovery URL and one
        matching callback registration on the TWC side so users land back in OWUI already signed in.
      </p>
      <div class="meta-grid">
        <div class="meta-card">
          <strong>Register This Callback In TWC</strong>
          <code id="callback-url"></code>
        </div>
        <div class="meta-card">
          <strong>Provider Key</strong>
          <code>/oauth/oidc/login</code>
        </div>
      </div>
      <div class="warning" id="persistence-warning" hidden>
        Saved values will work live right away, but restart persistence still requires
        <code>ENABLE_OAUTH_PERSISTENT_CONFIG=true</code> in the OWUI launch environment.
      </div>
    </section>

    <form id="sso-form">
      <section class="section">
        <h2>Login Behavior</h2>
        <p class="section-copy">Keep the user flow predictable while you validate the new SSO path end to end.</p>
        <div class="row-grid">
          <label class="toggle-row">
            <div class="label-block">
              <strong>Keep Password Login</strong>
              <span>Leave this on until you have confirmed the TWC redirect, callback, and OWUI session creation are all working.</span>
            </div>
            <div class="toggle-control"><input id="enable_password_auth" type="checkbox"></div>
          </label>
          <label class="toggle-row">
            <div class="label-block">
              <strong>Allow New SSO Sign Ups</strong>
              <span>Create OWUI users automatically the first time a valid TWC identity signs in.</span>
            </div>
            <div class="toggle-control"><input id="enable_oauth_signup" type="checkbox"></div>
          </label>
          <label class="toggle-row">
            <div class="label-block">
              <strong>Merge Existing Accounts By Email</strong>
              <span>Link an existing OWUI account when the incoming TWC email matches. Use this only if the TWC email claim is trustworthy.</span>
            </div>
            <div class="toggle-control"><input id="merge_accounts_by_email" type="checkbox"></div>
          </label>
        </div>
      </section>

      <section class="section">
        <h2>OpenID Connect</h2>
        <p class="section-copy">These fields define how OWUI talks to the TWC authorization server.</p>
        <div class="row-grid">
          <label class="field-row">
            <div class="label-block">
              <strong>OpenID Discovery URL</strong>
              <span>Paste the TWC discovery document URL, usually the <code>.well-known/openid-configuration</code> endpoint.</span>
            </div>
            <div class="field-stack"><input id="openid_provider_url" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Provider Label</strong>
              <span>This is the user-facing name OWUI shows for the SSO button and provider listing.</span>
            </div>
            <div class="field-stack"><input id="provider_name" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Client ID</strong>
              <span>The client identifier registered on the TWC authentication server.</span>
            </div>
            <div class="field-stack"><input id="client_id" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Client Secret</strong>
              <span>Leave this blank only if your TWC application genuinely uses a public-client flow.</span>
            </div>
            <div class="field-stack"><input id="client_secret" type="password" autocomplete="new-password"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Redirect URI Override</strong>
              <span>Leave empty to use the standard callback OWUI computes from its base URL.</span>
            </div>
            <div class="field-stack"><input id="redirect_uri" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Scopes</strong>
              <span>Keep at least <code>openid email profile</code> unless TWC needs a different scope string.</span>
            </div>
            <div class="field-stack"><input id="scopes" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Logout Endpoint</strong>
              <span>Optional. Set this only if TWC exposes an end-session endpoint that should run during sign-out.</span>
            </div>
            <div class="field-stack"><input id="end_session_endpoint" type="text" autocomplete="off"></div>
          </label>
        </div>
      </section>

      <section class="section">
        <h2>Claims & Protocol</h2>
        <p class="section-copy">Preset values use dropdowns so routine claim mappings are harder to mistype. Choose <em>Custom</em> only when TWC uses a non-standard field name.</p>
        <div class="row-grid">
          <label class="field-row">
            <div class="label-block">
              <strong>Token Auth Method</strong>
              <span>How OWUI authenticates to TWC at the token endpoint.</span>
            </div>
            <div class="field-stack">
              <select id="token_endpoint_auth_method">
                <option value="">Provider Default</option>
                <option value="client_secret_post">client_secret_post</option>
                <option value="client_secret_basic">client_secret_basic</option>
                <option value="none">none</option>
                <option value="custom">Custom</option>
              </select>
              <input id="token_endpoint_auth_method_custom" class="custom-field" type="text" placeholder="Custom token auth method" hidden>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>PKCE Method</strong>
              <span>Use <code>S256</code> if the TWC client registration expects PKCE. Leave blank to let the provider defaults stand.</span>
            </div>
            <div class="field-stack">
              <select id="code_challenge_method">
                <option value="">Disabled / Provider Default</option>
                <option value="S256">S256</option>
                <option value="plain">plain</option>
                <option value="custom">Custom</option>
              </select>
              <input id="code_challenge_method_custom" class="custom-field" type="text" placeholder="Custom PKCE method" hidden>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Subject Claim</strong>
              <span>The stable unique ID claim OWUI stores against the TWC identity.</span>
            </div>
            <div class="field-stack">
              <select id="sub_claim"></select>
              <input id="sub_claim_custom" class="custom-field" type="text" placeholder="Custom subject claim" hidden>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Display Name Claim</strong>
              <span>The claim OWUI uses for the signed-in user's visible name.</span>
            </div>
            <div class="field-stack">
              <select id="username_claim"></select>
              <input id="username_claim_custom" class="custom-field" type="text" placeholder="Custom display name claim" hidden>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Email Claim</strong>
              <span>The claim OWUI uses when linking or creating accounts.</span>
            </div>
            <div class="field-stack">
              <select id="email_claim"></select>
              <input id="email_claim_custom" class="custom-field" type="text" placeholder="Custom email claim" hidden>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Picture Claim</strong>
              <span>The optional profile image claim if TWC supplies one.</span>
            </div>
            <div class="field-stack">
              <select id="picture_claim"></select>
              <input id="picture_claim_custom" class="custom-field" type="text" placeholder="Custom picture claim" hidden>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Group Claim</strong>
              <span>The claim name OWUI should inspect if you later enable group mapping.</span>
            </div>
            <div class="field-stack">
              <select id="groups_claim"></select>
              <input id="groups_claim_custom" class="custom-field" type="text" placeholder="Custom group claim" hidden>
            </div>
          </label>
        </div>
      </section>

      <div class="actions">
        <div id="status" class="status"></div>
        <div class="button-row">
          <button class="secondary" id="reset-button" type="button">Reload Current Values</button>
          <button class="primary" id="save-button" type="submit">Save SSO Settings</button>
        </div>
      </div>
    </form>
  </div>

  <script>window.__OWUI_SSO_BOOT__ = __BOOTSTRAP__;</script>
  <script>
    (() => {
      const boot = window.__OWUI_SSO_BOOT__;
      const form = document.getElementById('sso-form');
      const statusEl = document.getElementById('status');
      const callbackUrlEl = document.getElementById('callback-url');
      const warningEl = document.getElementById('persistence-warning');
      const selectPresetMap = boot.config.claim_presets;

      const selectFieldIds = ['sub_claim', 'username_claim', 'email_claim', 'picture_claim', 'groups_claim'];
      const protocolFieldIds = ['token_endpoint_auth_method', 'code_challenge_method'];

      const setStatus = (message, kind = '') => {
        statusEl.textContent = message || '';
        statusEl.className = kind ? `status ${kind}` : 'status';
      };

      const setCustomVisibility = (fieldId) => {
        const select = document.getElementById(fieldId);
        const custom = document.getElementById(`${fieldId}_custom`);
        if (!select || !custom) {
          return;
        }
        custom.hidden = select.value !== 'custom';
      };

      const populatePresetSelect = (fieldId, presets, value) => {
        const select = document.getElementById(fieldId);
        const custom = document.getElementById(`${fieldId}_custom`);
        if (!select) {
          return;
        }
        select.innerHTML = '';
        for (const preset of presets) {
          const option = document.createElement('option');
          option.value = preset;
          option.textContent = preset;
          select.appendChild(option);
        }
        const customOption = document.createElement('option');
        customOption.value = 'custom';
        customOption.textContent = 'Custom';
        select.appendChild(customOption);

        if (value && !presets.includes(value)) {
          select.value = 'custom';
          if (custom) {
            custom.value = value;
          }
        } else {
          select.value = value || presets[0];
          if (custom) {
            custom.value = '';
          }
        }
        setCustomVisibility(fieldId);
      };

      const populateProtocolSelect = (fieldId, value) => {
        const select = document.getElementById(fieldId);
        const custom = document.getElementById(`${fieldId}_custom`);
        const selectValues = Array.from(select.options).map((option) => option.value);
        if (value && !selectValues.includes(value)) {
          select.value = 'custom';
          if (custom) {
            custom.value = value;
          }
        } else {
          select.value = value || '';
          if (custom) {
            custom.value = '';
          }
        }
        setCustomVisibility(fieldId);
      };

      const readSelectValue = (fieldId) => {
        const select = document.getElementById(fieldId);
        const custom = document.getElementById(`${fieldId}_custom`);
        if (!select) {
          return '';
        }
        if (select.value === 'custom') {
          return (custom?.value || '').trim();
        }
        return select.value;
      };

      const fillForm = (config) => {
        document.getElementById('openid_provider_url').value = config.openid_provider_url || '';
        document.getElementById('provider_name').value = config.provider_name || 'SSO';
        document.getElementById('client_id').value = config.client_id || '';
        document.getElementById('client_secret').value = config.client_secret || '';
        document.getElementById('redirect_uri').value = config.redirect_uri || '';
        document.getElementById('scopes').value = config.scopes || 'openid email profile';
        document.getElementById('end_session_endpoint').value = config.end_session_endpoint || '';
        document.getElementById('enable_oauth_signup').checked = !!config.enable_oauth_signup;
        document.getElementById('merge_accounts_by_email').checked = !!config.merge_accounts_by_email;
        document.getElementById('enable_password_auth').checked = !!config.enable_password_auth;

        callbackUrlEl.textContent = config.computed_callback_url || '';
        warningEl.hidden = !!config.oauth_persistent_config_enabled;

        populateProtocolSelect('token_endpoint_auth_method', config.token_endpoint_auth_method || '');
        populateProtocolSelect('code_challenge_method', config.code_challenge_method || '');

        for (const fieldId of selectFieldIds) {
          populatePresetSelect(fieldId, selectPresetMap[fieldId], config[fieldId] || '');
        }

        postHeight();
      };

      const buildPayload = () => ({
        openid_provider_url: document.getElementById('openid_provider_url').value.trim(),
        provider_name: document.getElementById('provider_name').value.trim(),
        client_id: document.getElementById('client_id').value.trim(),
        client_secret: document.getElementById('client_secret').value,
        redirect_uri: document.getElementById('redirect_uri').value.trim(),
        scopes: document.getElementById('scopes').value.trim(),
        end_session_endpoint: document.getElementById('end_session_endpoint').value.trim(),
        token_endpoint_auth_method: readSelectValue('token_endpoint_auth_method'),
        code_challenge_method: readSelectValue('code_challenge_method'),
        sub_claim: readSelectValue('sub_claim'),
        username_claim: readSelectValue('username_claim'),
        email_claim: readSelectValue('email_claim'),
        picture_claim: readSelectValue('picture_claim'),
        groups_claim: readSelectValue('groups_claim'),
        enable_oauth_signup: document.getElementById('enable_oauth_signup').checked,
        merge_accounts_by_email: document.getElementById('merge_accounts_by_email').checked,
        enable_password_auth: document.getElementById('enable_password_auth').checked,
      });

      const loadConfig = async () => {
        setStatus('Loading current SSO settings...');
        const response = await fetch(boot.endpoints.load, { credentials: 'same-origin' });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.detail || 'Failed to load SSO settings');
        }
        const config = await response.json();
        fillForm(config);
        setStatus('Current SSO settings loaded.');
      };

      const saveConfig = async () => {
        setStatus('Saving SSO settings...');
        const response = await fetch(boot.endpoints.save, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(buildPayload()),
        });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(error.detail || 'Failed to save SSO settings');
        }
        const config = await response.json();
        fillForm(config);
        setStatus('SSO settings saved. Restart OWUI only if you also changed launch-time environment values.', 'success');
      };

      const postHeight = () => {
        const height = Math.max(
          document.documentElement.scrollHeight,
          document.body.scrollHeight,
          document.documentElement.offsetHeight,
          document.body.offsetHeight
        );
        window.parent.postMessage({ type: 'owui-sso-height', height }, '*');
      };

      for (const fieldId of [...selectFieldIds, ...protocolFieldIds]) {
        const select = document.getElementById(fieldId);
        if (select) {
          select.addEventListener('change', () => {
            setCustomVisibility(fieldId);
            postHeight();
          });
        }
      }

      document.getElementById('reset-button').addEventListener('click', async () => {
        try {
          await loadConfig();
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
          await saveConfig();
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      const resizeObserver = new ResizeObserver(postHeight);
      resizeObserver.observe(document.body);
      window.addEventListener('load', postHeight);
      window.addEventListener('resize', postHeight);

      fillForm(boot.config);
      setStatus('SSO settings ready.');
      postHeight();
    })();
  </script>
</body>
</html>
"""
        .replace('__BOOTSTRAP__', bootstrap)
    )


class SSOConfigForm(BaseModel):
    openid_provider_url: str = ''
    provider_name: str = 'SSO'
    client_id: str = ''
    client_secret: str = ''
    redirect_uri: str = ''
    scopes: str = 'openid email profile'
    end_session_endpoint: str = ''
    token_endpoint_auth_method: str = ''
    code_challenge_method: str = ''
    sub_claim: str = 'sub'
    username_claim: str = 'name'
    email_claim: str = 'email'
    picture_claim: str = 'picture'
    groups_claim: str = 'groups'
    enable_oauth_signup: bool = False
    merge_accounts_by_email: bool = False
    enable_password_auth: bool = True


@router.get('/admin/config/sso')
async def get_sso_config(request: Request, user=Depends(get_admin_user)):
    return _get_sso_config_payload(request)


@router.post('/admin/config/sso')
async def update_sso_config(request: Request, form_data: SSOConfigForm, user=Depends(get_admin_user)):
    request.app.state.config.OPENID_PROVIDER_URL = _normalize_optional_text(form_data.openid_provider_url)
    request.app.state.config.OAUTH_PROVIDER_NAME = _normalize_optional_text(form_data.provider_name) or 'SSO'
    request.app.state.config.OAUTH_CLIENT_ID = _normalize_optional_text(form_data.client_id)
    request.app.state.config.OAUTH_CLIENT_SECRET = _normalize_optional_text(form_data.client_secret)
    request.app.state.config.OPENID_REDIRECT_URI = _normalize_optional_text(form_data.redirect_uri)
    request.app.state.config.OAUTH_SCOPES = _normalize_optional_text(form_data.scopes) or 'openid email profile'
    request.app.state.config.OPENID_END_SESSION_ENDPOINT = _normalize_optional_text(form_data.end_session_endpoint)
    request.app.state.config.OAUTH_TOKEN_ENDPOINT_AUTH_METHOD = _normalize_optional_text(
        form_data.token_endpoint_auth_method
    )
    request.app.state.config.OAUTH_CODE_CHALLENGE_METHOD = _normalize_optional_text(form_data.code_challenge_method)
    request.app.state.config.OAUTH_SUB_CLAIM = _normalize_optional_text(form_data.sub_claim) or 'sub'
    request.app.state.config.OAUTH_USERNAME_CLAIM = _normalize_optional_text(form_data.username_claim) or 'name'
    request.app.state.config.OAUTH_EMAIL_CLAIM = _normalize_optional_text(form_data.email_claim) or 'email'
    request.app.state.config.OAUTH_PICTURE_CLAIM = _normalize_optional_text(form_data.picture_claim) or 'picture'
    request.app.state.config.OAUTH_GROUPS_CLAIM = _normalize_optional_text(form_data.groups_claim) or 'groups'
    request.app.state.config.ENABLE_OAUTH_SIGNUP = form_data.enable_oauth_signup
    request.app.state.config.OAUTH_MERGE_ACCOUNTS_BY_EMAIL = form_data.merge_accounts_by_email
    request.app.state.config.ENABLE_PASSWORD_AUTH = form_data.enable_password_auth

    _refresh_oidc_runtime(request)
    return _get_sso_config_payload(request)


@router.get('/admin/config/sso/ui', response_class=HTMLResponse)
async def get_sso_config_ui(request: Request, user=Depends(get_admin_user)):
    return HTMLResponse(_build_sso_ui_html(request))


class LdapServerConfig(BaseModel):
    label: str
    host: str
    port: Optional[int] = None
    attribute_for_mail: str = 'mail'
    attribute_for_username: str = 'uid'
    app_dn: str
    app_dn_password: str
    search_base: str
    search_filters: str = ''
    use_tls: bool = True
    certificate_path: Optional[str] = None
    validate_cert: bool = True
    ciphers: Optional[str] = 'ALL'


@router.get('/admin/config/ldap/server', response_model=LdapServerConfig)
async def get_ldap_server(request: Request, user=Depends(get_admin_user)):
    return {
        'label': request.app.state.config.LDAP_SERVER_LABEL,
        'host': request.app.state.config.LDAP_SERVER_HOST,
        'port': request.app.state.config.LDAP_SERVER_PORT,
        'attribute_for_mail': request.app.state.config.LDAP_ATTRIBUTE_FOR_MAIL,
        'attribute_for_username': request.app.state.config.LDAP_ATTRIBUTE_FOR_USERNAME,
        'app_dn': request.app.state.config.LDAP_APP_DN,
        'app_dn_password': request.app.state.config.LDAP_APP_PASSWORD,
        'search_base': request.app.state.config.LDAP_SEARCH_BASE,
        'search_filters': request.app.state.config.LDAP_SEARCH_FILTERS,
        'use_tls': request.app.state.config.LDAP_USE_TLS,
        'certificate_path': request.app.state.config.LDAP_CA_CERT_FILE,
        'validate_cert': request.app.state.config.LDAP_VALIDATE_CERT,
        'ciphers': request.app.state.config.LDAP_CIPHERS,
    }


@router.post('/admin/config/ldap/server')
async def update_ldap_server(request: Request, form_data: LdapServerConfig, user=Depends(get_admin_user)):
    required_fields = [
        'label',
        'host',
        'attribute_for_mail',
        'attribute_for_username',
        'search_base',
    ]
    for key in required_fields:
        value = getattr(form_data, key)
        if not value:
            raise HTTPException(400, detail=ERROR_MESSAGES.REQUIRED_FIELD_EMPTY(key))

    request.app.state.config.LDAP_SERVER_LABEL = form_data.label
    request.app.state.config.LDAP_SERVER_HOST = form_data.host
    request.app.state.config.LDAP_SERVER_PORT = form_data.port
    request.app.state.config.LDAP_ATTRIBUTE_FOR_MAIL = form_data.attribute_for_mail
    request.app.state.config.LDAP_ATTRIBUTE_FOR_USERNAME = form_data.attribute_for_username
    request.app.state.config.LDAP_APP_DN = form_data.app_dn or ''
    request.app.state.config.LDAP_APP_PASSWORD = form_data.app_dn_password or ''
    request.app.state.config.LDAP_SEARCH_BASE = form_data.search_base
    request.app.state.config.LDAP_SEARCH_FILTERS = form_data.search_filters
    request.app.state.config.LDAP_USE_TLS = form_data.use_tls
    request.app.state.config.LDAP_CA_CERT_FILE = form_data.certificate_path
    request.app.state.config.LDAP_VALIDATE_CERT = form_data.validate_cert
    request.app.state.config.LDAP_CIPHERS = form_data.ciphers

    return {
        'label': request.app.state.config.LDAP_SERVER_LABEL,
        'host': request.app.state.config.LDAP_SERVER_HOST,
        'port': request.app.state.config.LDAP_SERVER_PORT,
        'attribute_for_mail': request.app.state.config.LDAP_ATTRIBUTE_FOR_MAIL,
        'attribute_for_username': request.app.state.config.LDAP_ATTRIBUTE_FOR_USERNAME,
        'app_dn': request.app.state.config.LDAP_APP_DN,
        'app_dn_password': request.app.state.config.LDAP_APP_PASSWORD,
        'search_base': request.app.state.config.LDAP_SEARCH_BASE,
        'search_filters': request.app.state.config.LDAP_SEARCH_FILTERS,
        'use_tls': request.app.state.config.LDAP_USE_TLS,
        'certificate_path': request.app.state.config.LDAP_CA_CERT_FILE,
        'validate_cert': request.app.state.config.LDAP_VALIDATE_CERT,
        'ciphers': request.app.state.config.LDAP_CIPHERS,
    }


@router.get('/admin/config/ldap')
async def get_ldap_config(request: Request, user=Depends(get_admin_user)):
    return {'ENABLE_LDAP': request.app.state.config.ENABLE_LDAP}


class LdapConfigForm(BaseModel):
    enable_ldap: Optional[bool] = None


@router.post('/admin/config/ldap')
async def update_ldap_config(request: Request, form_data: LdapConfigForm, user=Depends(get_admin_user)):
    request.app.state.config.ENABLE_LDAP = form_data.enable_ldap
    return {'ENABLE_LDAP': request.app.state.config.ENABLE_LDAP}


############################
# API Key
############################


# create api key
@router.post('/api_key', response_model=ApiKey)
async def generate_api_key(
    request: Request, user=Depends(get_current_user), db: AsyncSession = Depends(get_async_session)
):
    if not request.app.state.config.ENABLE_API_KEYS or (
        user.role != 'admin'
        and not await has_permission(user.id, 'features.api_keys', request.app.state.config.USER_PERMISSIONS)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.API_KEY_CREATION_NOT_ALLOWED,
        )

    api_key = create_api_key()
    success = await Users.update_user_api_key_by_id(user.id, api_key, db=db)

    if success:
        return {
            'api_key': api_key,
        }
    else:
        raise HTTPException(500, detail=ERROR_MESSAGES.CREATE_API_KEY_ERROR)


# delete api key
@router.delete('/api_key', response_model=bool)
async def delete_api_key(user=Depends(get_current_user), db: AsyncSession = Depends(get_async_session)):
    return await Users.delete_user_api_key_by_id(user.id, db=db)


# get api key
@router.get('/api_key', response_model=ApiKey)
async def get_api_key(user=Depends(get_current_user), db: AsyncSession = Depends(get_async_session)):
    api_key = await Users.get_user_api_key_by_id(user.id, db=db)
    if api_key:
        return {
            'api_key': api_key,
        }
    else:
        raise HTTPException(404, detail=ERROR_MESSAGES.API_KEY_NOT_FOUND)


############################
# Token Exchange
############################


class TokenExchangeForm(BaseModel):
    token: str  # OAuth access token from external provider


@router.post('/oauth/{provider}/token/exchange', response_model=SessionUserResponse)
async def token_exchange(
    request: Request,
    response: Response,
    provider: str,
    form_data: TokenExchangeForm,
    db: AsyncSession = Depends(get_async_session),
):
    """
    Exchange an external OAuth provider token for an OpenWebUI JWT.
    This endpoint is disabled by default. Set ENABLE_OAUTH_TOKEN_EXCHANGE=True to enable.
    """
    if not ENABLE_OAUTH_TOKEN_EXCHANGE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Token exchange is disabled',
        )

    provider = provider.lower()

    # Check if provider is configured
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.OAUTH_NOT_CONFIGURED(provider),
        )
    # Get the OAuth client for this provider
    oauth_manager = request.app.state.oauth_manager
    client = oauth_manager.get_client(provider)
    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.OAUTH_NOT_CONFIGURED(provider),
        )

    # Validate the token by calling the userinfo endpoint
    try:
        token_data = {'access_token': form_data.token, 'token_type': 'Bearer'}
        user_data = await client.userinfo(token=token_data)

        if not user_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid token or unable to fetch user info',
            )
    except Exception as e:
        log.warning(f'Token exchange failed for provider {provider}: {e}')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid token or unable to validate with provider',
        )

    # Extract user information from the token claims
    email_claim = request.app.state.config.OAUTH_EMAIL_CLAIM
    username_claim = request.app.state.config.OAUTH_USERNAME_CLAIM

    # Get sub claim
    sub = user_data.get(request.app.state.config.OAUTH_SUB_CLAIM or OAUTH_PROVIDERS[provider].get('sub_claim', 'sub'))
    if not sub:
        log.warning(f'Token exchange failed: sub claim missing from user data')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token missing required 'sub' claim",
        )

    email = user_data.get(email_claim, '')
    if not email:
        log.warning(f'Token exchange failed: email claim missing from user data')
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Token missing required email claim',
        )
    email = email.lower()

    # Enforce domain allowlist — same check as the normal OAuth callback
    if (
        '*' not in auth_manager_config.OAUTH_ALLOWED_DOMAINS
        and email.split('@')[-1] not in auth_manager_config.OAUTH_ALLOWED_DOMAINS
    ):
        log.warning(f'Token exchange denied: email domain not in allowed domains list')
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )

    # Try to find the user by OAuth sub
    user = await Users.get_user_by_oauth_sub(provider, sub, db=db)

    if not user and OAUTH_MERGE_ACCOUNTS_BY_EMAIL.value:
        # Try to find by email if merge is enabled
        user = await Users.get_user_by_email(email, db=db)
        if user:
            # Link the OAuth sub to this user
            await Users.update_user_oauth_by_id(user.id, provider, sub, db=db)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='User not found. Please sign in via the web interface first.',
        )

    return await create_session_response(request, user, db)
