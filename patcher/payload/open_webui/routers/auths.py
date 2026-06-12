from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
import urllib
import uuid
from ssl import CERT_NONE, CERT_REQUIRED, PROTOCOL_TLS
from typing import Any, List, Optional

from aiohttp import ClientSession
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from ldap3 import NONE, Connection, Server, Tls
from ldap3.utils.conv import escape_filter_chars
from open_webui.config import (
    ENABLE_LDAP,
    ENABLE_OAUTH_PERSISTENT_CONFIG,
    ENABLE_OAUTH_SIGNUP,
    ENABLE_PASSWORD_AUTH,
    OAUTH_AUTO_REDIRECT,
    OAUTH_MERGE_ACCOUNTS_BY_EMAIL,
    OAUTH_PROVIDERS,
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    OAUTH_CODE_CHALLENGE_METHOD,
    OAUTH_EMAIL_CLAIM,
    OAUTH_GROUPS_CLAIM,
    OAUTH_PICTURE_CLAIM,
    OAUTH_PROVIDER_NAME,
    OAUTH_SCOPES,
    OAUTH_SUB_CLAIM,
    OAUTH_TOKEN_ENDPOINT_AUTH_METHOD,
    OAUTH_USERNAME_CLAIM,
    OPENID_END_SESSION_ENDPOINT,
    OPENID_PROVIDER_URL,
    OPENID_REDIRECT_URI,
    TWC_AUTH_CLIENT_ID,
    TWC_AUTH_CLIENT_SECRET,
    TWC_AUTH_SCOPE,
    TWC_AUTH_SERVER_OVERRIDES,
    TWC_SAML_AUTHORIZE_URL,
    TWC_SAML_LOGIN_PATH,
    TWC_SAML_LOGIN_PORT,
    TWC_SAML_RETURN_URL_PARAMETER,
    TWC_SAML_TOKEN_PATH,
    TWC_SAML_TOKEN_URL,
    load_oauth_providers,
)
from open_webui.constants import ERROR_MESSAGES, WEBHOOK_MESSAGES
from open_webui.env import (
    AIOHTTP_CLIENT_SESSION_SSL,
    ENABLE_INITIAL_ADMIN_SIGNUP,
    ENABLE_OAUTH_TOKEN_EXCHANGE,
    WEBUI_AUTH,
    WEBUI_AUTH_COOKIE_SAME_SITE,
    WEBUI_AUTH_COOKIE_SECURE,
    WEBUI_AUTH_SIGNOUT_REDIRECT_URL,
    WEBUI_AUTH_TRUSTED_EMAIL_HEADER,
    WEBUI_AUTH_TRUSTED_GROUPS_HEADER,
    WEBUI_AUTH_TRUSTED_NAME_HEADER,
    WEBUI_AUTH_TRUSTED_ROLE_HEADER,
)
from open_webui.internal.db import get_async_session
from open_webui.models.auths import (
    AddUserForm,
    ApiKey,
    Auths,
    LdapForm,
    SigninForm,
    SigninResponse,
    SignupForm,
    Token,
    UpdatePasswordForm,
)
from open_webui.models.groups import Groups
from open_webui.models.oauth_sessions import OAuthSessions
from open_webui.models.users import (
    UpdateProfileForm,
    UserModel,
    UserProfileImageResponse,
    Users,
    UserStatus,
)
from open_webui.utils.access_control import get_permissions, has_permission
from open_webui.utils.auth import (
    create_api_key,
    create_token,
    decode_token,
    get_admin_user,
    get_current_user,
    get_http_authorization_cred,
    get_password_hash,
    get_verified_user,
    invalidate_token,
    validate_password,
    verify_password,
)
from open_webui.utils.groups import apply_default_group_assignment
from open_webui.utils.misc import parse_duration, validate_email_format
from open_webui.utils.oauth import auth_manager_config
from open_webui.utils.rate_limit import RateLimiter
from open_webui.utils.redis import get_redis_client
from open_webui.utils.webhook import post_webhook
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

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
    expires_at: int | None = None
    permissions: dict | None = None


class SessionUserInfoResponse(SessionUserResponse, UserStatus):
    bio: str | None = None
    gender: str | None = None
    date_of_birth: datetime.date | None = None


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

    response_data = {
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

    return response_data


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
    # Trusted-header auth mode delegates passwords to the reverse proxy
    if WEBUI_AUTH_TRUSTED_EMAIL_HEADER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.ACTION_PROHIBITED)
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

    if not ENABLE_PASSWORD_AUTH:
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
    if not ENABLE_PASSWORD_AUTH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ERROR_MESSAGES.ACTION_PROHIBITED,
        )

    if WEBUI_AUTH_TRUSTED_EMAIL_HEADER:
        if WEBUI_AUTH_TRUSTED_EMAIL_HEADER not in request.headers:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.INVALID_TRUSTED_HEADER)

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
        if has_users:
            if not request.app.state.config.ENABLE_SIGNUP or not request.app.state.config.ENABLE_LOGIN_FORM:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)
        # Don't gate the first admin on ENABLE_SIGNUP: it auto-disables and can persist stale across a DB reset.
        elif not request.app.state.config.ENABLE_LOGIN_FORM and not ENABLE_INITIAL_ADMIN_SIGNUP:
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


@router.post('/signout')
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
    ADMIN_EMAIL: str | None = None
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
    FOLDER_MAX_FILE_COUNT: int | str | None = None
    AUTOMATION_MAX_COUNT: int | str | None = None
    AUTOMATION_MIN_INTERVAL: int | str | None = None
    ENABLE_AUTOMATIONS: bool
    ENABLE_CHANNELS: bool
    ENABLE_CALENDAR: bool
    ENABLE_MEMORIES: bool
    ENABLE_NOTES: bool
    ENABLE_USER_WEBHOOKS: bool
    ENABLE_USER_STATUS: bool
    PENDING_USER_OVERLAY_TITLE: str | None = None
    PENDING_USER_OVERLAY_CONTENT: str | None = None
    RESPONSE_WATERMARK: str | None = None


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
    'user_id_claim': ['sub', 'oid', 'preferred_username', 'email'],
    'username_claim': ['preferred_username', 'name', 'given_name', 'email'],
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
    discovery_url = _normalize_optional_text(request.app.state.config.OPENID_PROVIDER_URL)
    user_id_claim = _normalize_optional_text(request.app.state.config.OAUTH_SUB_CLAIM) or 'sub'
    twc_auth_client_id = _normalize_optional_text(request.app.state.config.TWC_AUTH_CLIENT_ID)
    twc_auth_client_secret = _normalize_optional_text(request.app.state.config.TWC_AUTH_CLIENT_SECRET)
    twc_auth_scope = _normalize_optional_text(request.app.state.config.TWC_AUTH_SCOPE) or 'openid'
    return {
        'twc_auth_client_id': twc_auth_client_id,
        'twc_auth_client_secret': twc_auth_client_secret,
        'twc_auth_scope': twc_auth_scope,
        'twc_saml_authorize_url': _normalize_optional_text(request.app.state.config.TWC_SAML_AUTHORIZE_URL),
        'twc_saml_token_url': _normalize_optional_text(request.app.state.config.TWC_SAML_TOKEN_URL),
        'twc_saml_login_path': _normalize_optional_text(request.app.state.config.TWC_SAML_LOGIN_PATH)
        or '/authentication/authorize',
        'twc_saml_login_port': _normalize_optional_text(str(request.app.state.config.TWC_SAML_LOGIN_PORT or '8443')),
        'twc_saml_token_path': _normalize_optional_text(request.app.state.config.TWC_SAML_TOKEN_PATH)
        or '/authentication/api/token',
        'twc_saml_return_url_parameter': _normalize_optional_text(
            request.app.state.config.TWC_SAML_RETURN_URL_PARAMETER
        )
        or 'redirect_uri',
        'twc_auth_server_overrides': _normalize_optional_text(request.app.state.config.TWC_AUTH_SERVER_OVERRIDES)
        or '{}',
        'discovery_url': discovery_url,
        'openid_provider_url': discovery_url,
        'provider_name': _normalize_optional_text(request.app.state.config.OAUTH_PROVIDER_NAME) or 'oidc',
        'client_id': _normalize_optional_text(request.app.state.config.OAUTH_CLIENT_ID) or twc_auth_client_id,
        'client_secret': _normalize_optional_text(request.app.state.config.OAUTH_CLIENT_SECRET) or twc_auth_client_secret,
        'redirect_uri': _normalize_optional_text(request.app.state.config.OPENID_REDIRECT_URI),
        'computed_callback_url': _compute_sso_callback_url(request, request.app.state.config.OPENID_REDIRECT_URI),
        'scopes': _normalize_optional_text(request.app.state.config.OAUTH_SCOPES) or twc_auth_scope,
        'end_session_endpoint': _normalize_optional_text(request.app.state.config.OPENID_END_SESSION_ENDPOINT),
        'token_endpoint_auth_method': _normalize_optional_text(request.app.state.config.OAUTH_TOKEN_ENDPOINT_AUTH_METHOD),
        'code_challenge_method': _normalize_optional_text(request.app.state.config.OAUTH_CODE_CHALLENGE_METHOD),
        'user_id_claim': user_id_claim,
        'sub_claim': user_id_claim,
        'username_claim': _normalize_optional_text(request.app.state.config.OAUTH_USERNAME_CLAIM)
        or 'preferred_username',
        'email_claim': _normalize_optional_text(request.app.state.config.OAUTH_EMAIL_CLAIM) or 'email',
        'picture_claim': _normalize_optional_text(request.app.state.config.OAUTH_PICTURE_CLAIM) or 'picture',
        'groups_claim': _normalize_optional_text(request.app.state.config.OAUTH_GROUPS_CLAIM) or 'groups',
        'enable_oauth_signup': bool(request.app.state.config.ENABLE_OAUTH_SIGNUP),
        'oauth_auto_redirect': bool(request.app.state.config.OAUTH_AUTO_REDIRECT),
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
    .required-star {
      color: #b91c1c;
      font-weight: 800;
      margin-left: 4px;
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
        This panel uses the same TWC AuthServer variable names as Workbench, then maps them into
        Open WebUI's runtime login settings. Register one matching callback on the TWC side so users
        land back in OWUI already signed in.
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
              <strong>Auto Login With TWC</strong>
              <span>When OWUI is not logged in and TWC is the only OAuth provider, redirect straight to TWC instead of showing the local login page.</span>
            </div>
            <div class="toggle-control"><input id="oauth_auto_redirect" type="checkbox"></div>
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
        <h2>TWC AuthServer</h2>
        <p class="section-copy">These fields intentionally mirror the Workbench `.env` names.</p>
        <div class="row-grid">
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_AUTH_CLIENT_ID <span class="required-star" aria-label="required">*</span></strong>
              <span>One client id listed in TWC <code>authentication.client.ids</code>.</span>
            </div>
            <div class="field-stack"><input id="twc_auth_client_id" type="text" autocomplete="off" required></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_AUTH_CLIENT_SECRET <span class="required-star" aria-label="required">*</span></strong>
              <span>The TWC <code>authentication.client.secret</code> value. Workbench sends this as <code>X-Auth-Secret</code>.</span>
            </div>
            <div class="field-stack"><input id="twc_auth_client_secret" type="password" autocomplete="new-password" required></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_AUTH_SCOPE</strong>
              <span>Workbench defaults this to <code>openid</code>. Change only if your TWC AuthServer registration requires another scope.</span>
            </div>
            <div class="field-stack"><input id="twc_auth_scope" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_SAML_AUTHORIZE_URL</strong>
              <span>Optional complete authorize URL. Leave blank to derive <code>https://&lt;twc-host&gt;:8443/authentication/authorize</code>.</span>
            </div>
            <div class="field-stack"><input id="twc_saml_authorize_url" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_SAML_TOKEN_URL</strong>
              <span>Optional complete token URL. Leave blank to derive <code>/authentication/api/token</code>.</span>
            </div>
            <div class="field-stack"><input id="twc_saml_token_url" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_SAML_LOGIN_PATH</strong>
              <span>Used only when the authorize URL is blank.</span>
            </div>
            <div class="field-stack"><input id="twc_saml_login_path" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_SAML_LOGIN_PORT</strong>
              <span>Used only when the authorize URL is blank.</span>
            </div>
            <div class="field-stack"><input id="twc_saml_login_port" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_SAML_TOKEN_PATH</strong>
              <span>Used only when the token URL is blank.</span>
            </div>
            <div class="field-stack"><input id="twc_saml_token_path" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_SAML_RETURN_URL_PARAMETER</strong>
              <span>Workbench defaults this to <code>redirect_uri</code>.</span>
            </div>
            <div class="field-stack"><input id="twc_saml_return_url_parameter" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>TWC_AUTH_SERVER_OVERRIDES</strong>
              <span>Optional JSON object keyed by preset/server id, matching Workbench.</span>
            </div>
            <div class="field-stack"><input id="twc_auth_server_overrides" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>OWUI Discovery URL</strong>
              <span>Optional compatibility field for OWUI's built-in OIDC client. Leave blank if the dedicated TWC authorize/token fields are used later.</span>
            </div>
            <div class="field-stack"><input id="discovery_url" type="text" autocomplete="off"></div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>OWUI Redirect URI Override</strong>
              <span>Leave empty to use the standard callback OWUI computes from its base URL.</span>
            </div>
            <div class="field-stack"><input id="redirect_uri" type="text" autocomplete="off"></div>
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
              <strong>User ID Claim</strong>
              <span>The stable unique ID claim OWUI stores against the TWC identity.</span>
            </div>
            <div class="field-stack">
              <select id="user_id_claim"></select>
              <input id="user_id_claim_custom" class="custom-field" type="text" placeholder="Custom user ID claim" hidden>
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

      const selectFieldIds = ['user_id_claim', 'username_claim', 'email_claim', 'picture_claim', 'groups_claim'];
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
        document.getElementById('twc_auth_client_id').value = config.twc_auth_client_id || config.client_id || '';
        document.getElementById('twc_auth_client_secret').value = config.twc_auth_client_secret || config.client_secret || '';
        document.getElementById('twc_auth_scope').value = config.twc_auth_scope || config.scopes || 'openid';
        document.getElementById('twc_saml_authorize_url').value = config.twc_saml_authorize_url || '';
        document.getElementById('twc_saml_token_url').value = config.twc_saml_token_url || '';
        document.getElementById('twc_saml_login_path').value = config.twc_saml_login_path || '/authentication/authorize';
        document.getElementById('twc_saml_login_port').value = config.twc_saml_login_port || '8443';
        document.getElementById('twc_saml_token_path').value = config.twc_saml_token_path || '/authentication/api/token';
        document.getElementById('twc_saml_return_url_parameter').value = config.twc_saml_return_url_parameter || 'redirect_uri';
        document.getElementById('twc_auth_server_overrides').value = config.twc_auth_server_overrides || '{}';
        document.getElementById('discovery_url').value = config.discovery_url || config.openid_provider_url || '';
        document.getElementById('redirect_uri').value = config.redirect_uri || '';
        document.getElementById('enable_oauth_signup').checked = !!config.enable_oauth_signup;
        document.getElementById('oauth_auto_redirect').checked = !!config.oauth_auto_redirect;
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
        twc_auth_client_id: document.getElementById('twc_auth_client_id').value.trim(),
        twc_auth_client_secret: document.getElementById('twc_auth_client_secret').value,
        twc_auth_scope: document.getElementById('twc_auth_scope').value.trim(),
        twc_saml_authorize_url: document.getElementById('twc_saml_authorize_url').value.trim(),
        twc_saml_token_url: document.getElementById('twc_saml_token_url').value.trim(),
        twc_saml_login_path: document.getElementById('twc_saml_login_path').value.trim(),
        twc_saml_login_port: document.getElementById('twc_saml_login_port').value.trim(),
        twc_saml_token_path: document.getElementById('twc_saml_token_path').value.trim(),
        twc_saml_return_url_parameter: document.getElementById('twc_saml_return_url_parameter').value.trim(),
        twc_auth_server_overrides: document.getElementById('twc_auth_server_overrides').value.trim(),
        discovery_url: document.getElementById('discovery_url').value.trim(),
        redirect_uri: document.getElementById('redirect_uri').value.trim(),
        token_endpoint_auth_method: readSelectValue('token_endpoint_auth_method'),
        code_challenge_method: readSelectValue('code_challenge_method'),
        user_id_claim: readSelectValue('user_id_claim'),
        username_claim: readSelectValue('username_claim'),
        email_claim: readSelectValue('email_claim'),
        picture_claim: readSelectValue('picture_claim'),
        groups_claim: readSelectValue('groups_claim'),
        enable_oauth_signup: document.getElementById('enable_oauth_signup').checked,
        oauth_auto_redirect: document.getElementById('oauth_auto_redirect').checked,
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
    twc_auth_client_id: str = ''
    twc_auth_client_secret: str = ''
    twc_auth_scope: str = 'openid'
    twc_saml_authorize_url: str = ''
    twc_saml_token_url: str = ''
    twc_saml_login_path: str = '/authentication/authorize'
    twc_saml_login_port: str = '8443'
    twc_saml_token_path: str = '/authentication/api/token'
    twc_saml_return_url_parameter: str = 'redirect_uri'
    twc_auth_server_overrides: str = '{}'
    discovery_url: str = ''
    openid_provider_url: str = ''
    provider_name: str = 'oidc'
    client_id: str = ''
    client_secret: str = ''
    redirect_uri: str = ''
    scopes: str = 'openid email profile'
    end_session_endpoint: str = ''
    token_endpoint_auth_method: str = ''
    code_challenge_method: str = ''
    user_id_claim: str = 'sub'
    sub_claim: str = ''
    username_claim: str = 'preferred_username'
    email_claim: str = 'email'
    picture_claim: str = 'picture'
    groups_claim: str = 'groups'
    enable_oauth_signup: bool = False
    oauth_auto_redirect: bool = False
    merge_accounts_by_email: bool = False
    enable_password_auth: bool = True


@router.get('/admin/config/sso')
async def get_sso_config(request: Request, user=Depends(get_admin_user)):
    return _get_sso_config_payload(request)


@router.post('/admin/config/sso')
async def update_sso_config(request: Request, form_data: SSOConfigForm, user=Depends(get_admin_user)):
    discovery_url = _normalize_optional_text(form_data.discovery_url) or _normalize_optional_text(
        form_data.openid_provider_url
    )
    user_id_claim = _normalize_optional_text(form_data.user_id_claim) or _normalize_optional_text(form_data.sub_claim)
    twc_auth_client_id = _normalize_optional_text(form_data.twc_auth_client_id) or _normalize_optional_text(
        form_data.client_id
    )
    twc_auth_client_secret = _normalize_optional_text(form_data.twc_auth_client_secret) or _normalize_optional_text(
        form_data.client_secret
    )
    twc_auth_scope = _normalize_optional_text(form_data.twc_auth_scope) or _normalize_optional_text(form_data.scopes)
    twc_auth_scope = twc_auth_scope or 'openid'

    missing_required = []
    if not twc_auth_client_id:
        missing_required.append('TWC_AUTH_CLIENT_ID')
    if not twc_auth_client_secret:
        missing_required.append('TWC_AUTH_CLIENT_SECRET')
    if missing_required:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Missing required TWC SSO setting(s): {", ".join(missing_required)}',
        )

    request.app.state.config.TWC_AUTH_CLIENT_ID = twc_auth_client_id
    request.app.state.config.TWC_AUTH_CLIENT_SECRET = twc_auth_client_secret
    request.app.state.config.TWC_AUTH_SCOPE = twc_auth_scope
    request.app.state.config.TWC_SAML_AUTHORIZE_URL = _normalize_optional_text(form_data.twc_saml_authorize_url)
    request.app.state.config.TWC_SAML_TOKEN_URL = _normalize_optional_text(form_data.twc_saml_token_url)
    request.app.state.config.TWC_SAML_LOGIN_PATH = (
        _normalize_optional_text(form_data.twc_saml_login_path) or '/authentication/authorize'
    )
    request.app.state.config.TWC_SAML_LOGIN_PORT = _normalize_optional_text(form_data.twc_saml_login_port) or '8443'
    request.app.state.config.TWC_SAML_TOKEN_PATH = (
        _normalize_optional_text(form_data.twc_saml_token_path) or '/authentication/api/token'
    )
    request.app.state.config.TWC_SAML_RETURN_URL_PARAMETER = (
        _normalize_optional_text(form_data.twc_saml_return_url_parameter) or 'redirect_uri'
    )
    request.app.state.config.TWC_AUTH_SERVER_OVERRIDES = (
        _normalize_optional_text(form_data.twc_auth_server_overrides) or '{}'
    )
    request.app.state.config.OPENID_PROVIDER_URL = discovery_url
    request.app.state.config.OAUTH_PROVIDER_NAME = _normalize_optional_text(form_data.provider_name) or 'oidc'
    request.app.state.config.OAUTH_CLIENT_ID = twc_auth_client_id
    request.app.state.config.OAUTH_CLIENT_SECRET = twc_auth_client_secret
    request.app.state.config.OPENID_REDIRECT_URI = _normalize_optional_text(form_data.redirect_uri)
    request.app.state.config.OAUTH_SCOPES = twc_auth_scope
    request.app.state.config.OPENID_END_SESSION_ENDPOINT = _normalize_optional_text(form_data.end_session_endpoint)
    request.app.state.config.OAUTH_TOKEN_ENDPOINT_AUTH_METHOD = _normalize_optional_text(
        form_data.token_endpoint_auth_method
    )
    request.app.state.config.OAUTH_CODE_CHALLENGE_METHOD = _normalize_optional_text(form_data.code_challenge_method)
    request.app.state.config.OAUTH_SUB_CLAIM = user_id_claim or 'sub'
    request.app.state.config.OAUTH_USERNAME_CLAIM = (
        _normalize_optional_text(form_data.username_claim) or 'preferred_username'
    )
    request.app.state.config.OAUTH_EMAIL_CLAIM = _normalize_optional_text(form_data.email_claim) or 'email'
    request.app.state.config.OAUTH_PICTURE_CLAIM = _normalize_optional_text(form_data.picture_claim) or 'picture'
    request.app.state.config.OAUTH_GROUPS_CLAIM = _normalize_optional_text(form_data.groups_claim) or 'groups'
    request.app.state.config.ENABLE_OAUTH_SIGNUP = form_data.enable_oauth_signup
    request.app.state.config.OAUTH_AUTO_REDIRECT = form_data.oauth_auto_redirect
    request.app.state.config.OAUTH_MERGE_ACCOUNTS_BY_EMAIL = form_data.merge_accounts_by_email
    request.app.state.config.ENABLE_PASSWORD_AUTH = form_data.enable_password_auth

    _refresh_oidc_runtime(request)
    return _get_sso_config_payload(request)


@router.get('/admin/config/sso/ui', response_class=HTMLResponse)
async def get_sso_config_ui(request: Request, user=Depends(get_admin_user)):
    return HTMLResponse(_build_sso_ui_html(request))


TWC_WORKBENCH_SETTINGS_KEY = 'twc_workbench'


def _normalize_workbench_base_url(value: Optional[str]) -> str:
    normalized = _normalize_optional_text(value)
    if not normalized:
        return ''

    if '://' not in normalized:
        if normalized.startswith(('localhost', '127.0.0.1', '0.0.0.0', '[::1]')):
            normalized = f'http://{normalized}'
        else:
            normalized = f'https://{normalized}'

    return normalized.rstrip('/')


def _is_local_workbench_url(base_url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(base_url)
    except Exception:
        return False

    hostname = (parsed.hostname or '').strip().lower()
    return hostname in {'localhost', '127.0.0.1', '0.0.0.0', '::1'}


def _get_workbench_user_settings(user) -> dict:
    raw_settings = user.settings or {}
    ui_settings = raw_settings.get('ui') or {}
    workbench_settings = ui_settings.get(TWC_WORKBENCH_SETTINGS_KEY) or {}

    base_url = _normalize_workbench_base_url(workbench_settings.get('base_url'))
    verify_tls = workbench_settings.get('verify_tls')
    if verify_tls is None:
        verify_tls = not _is_local_workbench_url(base_url)

    return {
        'base_url': base_url,
        'api_key': _normalize_optional_text(workbench_settings.get('api_key')),
        'verify_tls': bool(verify_tls),
        'owui_model_id': _normalize_optional_text(workbench_settings.get('owui_model_id')),
        'owui_function_id': _normalize_optional_text(workbench_settings.get('owui_function_id')),
        'workbench_server_id': _normalize_optional_text(workbench_settings.get('workbench_server_id')),
        'workbench_project_id': _normalize_optional_text(workbench_settings.get('workbench_project_id')),
        'workbench_branch_id': _normalize_optional_text(workbench_settings.get('workbench_branch_id')),
        'workbench_branch_model_id': _normalize_optional_text(workbench_settings.get('workbench_branch_model_id')),
        'updated_at': _normalize_optional_text(workbench_settings.get('updated_at')),
    }


def _workbench_api_key_hint(api_key: str) -> str:
    token = _normalize_optional_text(api_key)
    if not token:
        return ''
    if len(token) <= 8:
        return 'Stored'
    return f'{token[:4]}...{token[-4:]}'


def _build_workbench_context_payload(user) -> dict:
    settings = _get_workbench_user_settings(user)
    return {
        'user': {
            'id': user.id,
            'email': user.email,
            'name': user.name,
            'role': user.role,
        },
        'settings': {
            'base_url': settings['base_url'],
            'verify_tls': settings['verify_tls'],
            'has_api_key': bool(settings['api_key']),
            'api_key_hint': _workbench_api_key_hint(settings['api_key']),
            'owui_model_id': settings['owui_model_id'],
            'owui_function_id': settings['owui_function_id'],
            'workbench_server_id': settings['workbench_server_id'],
            'workbench_project_id': settings['workbench_project_id'],
            'workbench_branch_id': settings['workbench_branch_id'],
            'workbench_branch_model_id': settings['workbench_branch_model_id'],
            'updated_at': settings['updated_at'] or None,
        },
        'notes': {
            'same_sso': 'The current OWUI session user is shown here. The Workbench API key should belong to this same SSO user.',
            'local_host_hint': 'When Workbench runs on this same device, the localhost hop stays fast and the bigger cost remains the model call itself.',
        },
    }


class WorkbenchConfigForm(BaseModel):
    base_url: str = ''
    api_key: str = ''
    verify_tls: bool = False
    owui_model_id: str = ''
    owui_function_id: str = ''
    workbench_server_id: str = ''
    workbench_project_id: str = ''
    workbench_branch_id: str = ''
    workbench_branch_model_id: str = ''


def _merge_workbench_ui_settings(user, payload: dict) -> dict:
    merged_settings = dict(user.settings or {})
    merged_ui_settings = dict(merged_settings.get('ui') or {})
    existing_workbench = dict(merged_ui_settings.get(TWC_WORKBENCH_SETTINGS_KEY) or {})
    existing_workbench.update(payload)
    merged_ui_settings[TWC_WORKBENCH_SETTINGS_KEY] = existing_workbench
    merged_settings['ui'] = merged_ui_settings
    return merged_settings


def _quote_workbench_path(value: str) -> str:
    return urllib.parse.quote(str(value), safe='')


async def _workbench_api_json(user, method: str, path: str) -> Any:
    settings = _get_workbench_user_settings(user)
    if not settings['base_url']:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Save a Workbench base URL first.')
    if not settings['api_key']:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Save a Workbench API key first.')

    upstream_url = f"{settings['base_url']}{path}"
    ssl_value = AIOHTTP_CLIENT_SESSION_SSL if settings['verify_tls'] else False

    try:
        async with ClientSession(trust_env=True) as session:
            async with session.request(
                method,
                upstream_url,
                headers={
                    'Authorization': f"Bearer {settings['api_key']}",
                    'Accept': 'application/json',
                },
                ssl=ssl_value,
            ) as response:
                raw_text = await response.text()
                parsed_json = None
                if raw_text:
                    try:
                        parsed_json = json.loads(raw_text)
                    except Exception:
                        parsed_json = None

                if response.status >= 400:
                    detail = response.reason
                    if isinstance(parsed_json, dict):
                        detail = parsed_json.get('detail') or parsed_json.get('message') or detail
                    elif raw_text:
                        detail = raw_text

                    status_code = (
                        response.status
                        if response.status in {400, 401, 403, 404, 409, 422}
                        else status.HTTP_502_BAD_GATEWAY
                    )
                    raise HTTPException(status_code=status_code, detail=f'Workbench: {detail}')

                if parsed_json is None:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail='Workbench returned a non-JSON response.',
                    )

                return parsed_json
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'Workbench request failed: {exc}',
        ) from exc


def _build_workbench_ui_html(user) -> str:
    bootstrap = json.dumps(
        {
            'context': _build_workbench_context_payload(user),
            'endpoints': {
                'context': '/api/v1/auths/workbench/context',
                'manifest': '/api/v1/auths/workbench/cache/manifest',
                'servers': '/api/v1/auths/workbench/cache/servers',
                'projects': '/api/v1/auths/workbench/cache/projects',
                'branch_summary': '/api/v1/auths/workbench/cache/branch/summary',
                'branch_models': '/api/v1/auths/workbench/cache/branch/models',
                'owui_models': '/api/models',
                'owui_functions': '/api/v1/functions/',
            },
        }
    )

    return (
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OWUI Workbench Bridge</title>
  <style>
    :root {
      --page-bg: linear-gradient(180deg, #f8fafc 0%, #e2e8f0 100%);
      --panel-bg: rgba(255, 255, 255, 0.92);
      --panel-border: rgba(148, 163, 184, 0.26);
      --text-main: #0f172a;
      --text-muted: #475569;
      --accent: #0f766e;
      --accent-strong: #155e75;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --shadow: 0 20px 60px rgba(15, 23, 42, 0.1);
      --warning-bg: rgba(251, 191, 36, 0.12);
      --warning-text: #92400e;
      --success-text: #166534;
      --error-text: #b91c1c;
    }
    * { box-sizing: border-box; }
    html, body { min-height: 100%; }
    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--text-main);
      background: var(--page-bg);
    }
    .shell {
      width: min(1180px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 20px 0 28px;
    }
    .hero,
    .panel {
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 26px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }
    .hero {
      padding: 28px;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 42%),
        radial-gradient(circle at top right, rgba(21, 94, 117, 0.12), transparent 36%),
        var(--panel-bg);
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
      line-height: 1.03;
      letter-spacing: -0.03em;
    }
    .lede {
      margin: 0;
      max-width: 76ch;
      color: var(--text-muted);
      font-size: 15px;
      line-height: 1.7;
    }
    .meta-grid,
    .summary-grid,
    .section-grid {
      display: grid;
      gap: 16px;
    }
    .meta-grid {
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      margin-top: 22px;
    }
    .summary-grid {
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 16px;
    }
    .section-grid {
      margin-top: 18px;
    }
    .meta-card,
    .summary-card {
      border: 1px solid rgba(148, 163, 184, 0.24);
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.7);
      padding: 18px 20px;
    }
    .meta-card strong,
    .summary-card strong {
      display: block;
      margin-bottom: 8px;
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .meta-card code,
    .summary-value code {
      display: inline-block;
      padding: 2px 6px;
      border-radius: 8px;
      background: rgba(15, 23, 42, 0.05);
      font-family: Consolas, monospace;
      font-size: 12px;
    }
    .meta-card .value,
    .summary-value {
      font-size: 14px;
      font-weight: 600;
      line-height: 1.55;
      overflow-wrap: anywhere;
    }
    .panel {
      padding: 24px;
      margin-top: 18px;
    }
    .panel h2 {
      margin: 0 0 6px;
      font-size: 24px;
      letter-spacing: -0.02em;
    }
    .panel p.copy {
      margin: 0 0 18px;
      color: var(--text-muted);
      font-size: 14px;
      line-height: 1.65;
    }
    .field-row {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(260px, 0.95fr);
      gap: 16px;
      align-items: start;
      padding: 18px;
      border-radius: 20px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      background: rgba(255, 255, 255, 0.7);
    }
    .label-block strong {
      display: block;
      margin-bottom: 6px;
      font-size: 15px;
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
      background: rgba(255, 255, 255, 0.96);
      color: var(--text-main);
      font: inherit;
      outline: none;
    }
    input:focus,
    select:focus {
      border-color: rgba(15, 118, 110, 0.72);
      box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12);
    }
    select:disabled,
    input:disabled {
      cursor: not-allowed;
      opacity: 0.7;
      background: rgba(226, 232, 240, 0.55);
    }
    .field-stack {
      display: grid;
      gap: 10px;
    }
    .toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      background: rgba(255, 255, 255, 0.72);
    }
    .toggle-row input {
      width: 18px;
      height: 18px;
      accent-color: var(--accent);
    }
    .hint,
    .warning {
      padding: 14px 16px;
      border-radius: 18px;
      font-size: 13px;
      line-height: 1.55;
    }
    .hint {
      background: rgba(15, 118, 110, 0.08);
      color: var(--accent-strong);
      border: 1px solid rgba(15, 118, 110, 0.18);
    }
    .warning {
      background: var(--warning-bg);
      color: var(--warning-text);
      border: 1px solid rgba(245, 158, 11, 0.22);
    }
    .status {
      min-height: 24px;
      font-size: 13px;
      color: var(--text-muted);
    }
    .status.success { color: var(--success-text); }
    .status.error { color: var(--error-text); }
    .pill-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .pill {
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(15, 118, 110, 0.1);
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }
    .actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-top: 18px;
    }
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
      background: linear-gradient(135deg, var(--accent), var(--accent-strong));
      color: white;
      box-shadow: 0 14px 30px rgba(21, 94, 117, 0.24);
    }
    button.secondary {
      background: rgba(255, 255, 255, 0.8);
      color: var(--text-main);
      border: 1px solid rgba(148, 163, 184, 0.28);
    }
    @media (max-width: 940px) {
      .shell { width: min(100vw - 16px, 1180px); }
      .hero, .panel { padding: 20px; border-radius: 22px; }
      .field-row { grid-template-columns: 1fr; }
      .actions { flex-direction: column; align-items: flex-start; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">OWUI Workbench Bridge</div>
      <h1>Route OWUI model work through your Workbench cache without sending users back to a separate chat.</h1>
      <p class="lede">
        Use this panel to bind the current OWUI user to a local Workbench cache connection, pick the OWUI model or function you want in play,
        and lock the engineering context down to one stored server, project, branch, and branch model.
      </p>
      <div class="meta-grid">
        <div class="meta-card">
          <strong>Current OWUI User</strong>
          <div class="value" id="owui-user-card"></div>
        </div>
        <div class="meta-card">
          <strong>Workbench API Identity</strong>
          <div class="value" id="workbench-user-card">Not connected yet.</div>
        </div>
        <div class="meta-card">
          <strong>Saved Connection</strong>
          <div class="value" id="connection-card">No Workbench base URL saved yet.</div>
        </div>
      </div>
      <div class="warning" style="margin-top:16px;" id="same-sso-note"></div>
    </section>

    <form id="workbench-form">
      <section class="panel">
        <h2>Connection</h2>
        <p class="copy">Keep the bridge light. Workbench remains the source of truth for TWC cache data, and OWUI stays the primary chat surface.</p>
        <div class="section-grid">
          <label class="field-row">
            <div class="label-block">
              <strong>Workbench Base URL</strong>
              <span>Point this at the Workbench backend on this machine, for example <code>http://127.0.0.1:8000</code> or the local Caddy URL you already use.</span>
            </div>
            <div class="field-stack">
              <input id="base_url" type="text" placeholder="http://127.0.0.1:8000" autocomplete="off">
              <label class="toggle-row">
                <span>Verify TLS certificates for this Workbench URL</span>
                <input id="verify_tls" type="checkbox">
              </label>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Workbench Cache API Key</strong>
              <span>Use a Workbench API key created by this same user. The full key is never shown again after save; OWUI only tells you whether one is already stored.</span>
            </div>
            <div class="field-stack">
              <input id="api_key" type="password" autocomplete="new-password" placeholder="Paste Workbench API key only when saving or rotating it">
              <div class="hint" id="api-key-hint">No Workbench API key is currently stored for this OWUI user.</div>
            </div>
          </label>
        </div>
      </section>

      <section class="panel">
        <h2>OWUI Routing</h2>
        <p class="copy">Choose the OWUI-side model and optional function/agent slot you want this engineering context to target.</p>
        <div class="section-grid">
          <label class="field-row">
            <div class="label-block">
              <strong>OWUI Model</strong>
              <span>All models visible to this signed-in OWUI user appear here.</span>
            </div>
            <div class="field-stack">
              <select id="owui_model_id"></select>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>OWUI Agent / Function</strong>
              <span>Optional. Pick the function or agent-like function set you want paired with the model when you use this Workbench context.</span>
            </div>
            <div class="field-stack">
              <select id="owui_function_id"></select>
            </div>
          </label>
        </div>
      </section>

      <section class="panel">
        <h2>Workbench Context</h2>
        <p class="copy">Selections come directly from the Workbench cache API. Project options include their cached branch list so branch selection stays error-proof.</p>
        <div class="section-grid">
          <label class="field-row">
            <div class="label-block">
              <strong>TWC Server</strong>
              <span>Choose which cached Workbench server snapshot this chat context should read from.</span>
            </div>
            <div class="field-stack">
              <select id="workbench_server_id"></select>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Project</strong>
              <span>Only projects visible to this Workbench API user are shown.</span>
            </div>
            <div class="field-stack">
              <select id="workbench_project_id"></select>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Branch</strong>
              <span>Branch options are sourced from the selected cached project entry.</span>
            </div>
            <div class="field-stack">
              <select id="workbench_branch_id"></select>
            </div>
          </label>
          <label class="field-row">
            <div class="label-block">
              <strong>Branch Model</strong>
              <span>Optional. Narrow the context to one cached branch model when you do not want the whole branch in play.</span>
            </div>
            <div class="field-stack">
              <select id="workbench_branch_model_id"></select>
            </div>
          </label>
        </div>
        <div class="summary-grid" id="branch-summary-grid">
          <div class="summary-card">
            <strong>Status</strong>
            <div class="summary-value" id="summary-status">Pick a branch to inspect.</div>
          </div>
          <div class="summary-card">
            <strong>Revision</strong>
            <div class="summary-value" id="summary-revision">-</div>
          </div>
          <div class="summary-card">
            <strong>Models</strong>
            <div class="summary-value" id="summary-model-count">-</div>
          </div>
          <div class="summary-card">
            <strong>Elements</strong>
            <div class="summary-value" id="summary-element-count">-</div>
          </div>
          <div class="summary-card">
            <strong>Updated</strong>
            <div class="summary-value" id="summary-updated">-</div>
          </div>
        </div>
        <div class="pill-row" id="manifest-scopes" style="margin-top:14px;"></div>
      </section>

      <div class="actions">
        <div id="status" class="status"></div>
        <div class="button-row">
          <button class="secondary" id="reload-button" type="button">Reload Lists</button>
          <button class="secondary" id="clear-button" type="button">Clear Saved Bridge</button>
          <button class="primary" id="save-button" type="submit">Save Workbench Bridge</button>
        </div>
      </div>
    </form>
  </div>

  <script>window.__OWUI_WORKBENCH_BOOT__ = __BOOTSTRAP__;</script>
  <script>
    (() => {
      const boot = window.__OWUI_WORKBENCH_BOOT__;
      const state = {
        context: boot.context,
        models: [],
        functions: [],
        manifest: null,
        servers: [],
        projects: [],
        branchModels: [],
        branchSummary: null,
      };

      const byId = (id) => document.getElementById(id);
      const statusEl = byId('status');

      const authHeaders = () => {
        const token = window.localStorage?.getItem('token');
        return token ? { Authorization: `Bearer ${token}` } : {};
      };

      const fetchJson = async (url, options = {}) => {
        const response = await fetch(url, {
          credentials: 'same-origin',
          headers: {
            Accept: 'application/json',
            ...authHeaders(),
            ...(options.headers || {}),
          },
          ...options,
        });
        if (!response.ok) {
          let detail = response.statusText || `HTTP ${response.status}`;
          try {
            const payload = await response.json();
            detail = payload.detail || payload.message || detail;
          } catch (error) {
            // Ignore body parsing failures here.
          }
          throw new Error(detail);
        }
        return response.json();
      };

      const setStatus = (message, kind = '') => {
        statusEl.textContent = message || '';
        statusEl.className = kind ? `status ${kind}` : 'status';
      };

      const formatDate = (value) => {
        if (!value) {
          return '-';
        }
        try {
          return new Date(value).toLocaleString();
        } catch (error) {
          return value;
        }
      };

      const optionLabel = (value, fallback = '') => {
        if (value === null || value === undefined || value === '') {
          return fallback;
        }
        return String(value);
      };

      const populateSelect = (selectId, entries, selectedValue, placeholder, mapOption) => {
        const select = byId(selectId);
        select.innerHTML = '';

        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        placeholderOption.textContent = placeholder;
        select.appendChild(placeholderOption);

        for (const entry of entries) {
          const optionData = mapOption(entry);
          const option = document.createElement('option');
          option.value = optionData.value;
          option.textContent = optionData.label;
          select.appendChild(option);
        }

        if (selectedValue && entries.some((entry) => mapOption(entry).value === selectedValue)) {
          select.value = selectedValue;
        } else {
          select.value = '';
        }
      };

      const updateMetaCards = () => {
        const context = state.context || { user: {}, settings: {} };
        const settings = context.settings || {};
        const user = context.user || {};
        byId('owui-user-card').innerHTML = `
          <div>${optionLabel(user.name, 'Unknown user')}</div>
          <div style="color:var(--text-muted);font-weight:500;">${optionLabel(user.email, 'No email')} · ${optionLabel(user.role, 'user')}</div>
        `;
        byId('same-sso-note').textContent = context.notes?.same_sso || '';
        byId('connection-card').innerHTML = settings.base_url
          ? `<div>${settings.base_url}</div><div style="color:var(--text-muted);font-weight:500;">Stored key: ${settings.api_key_hint || 'hidden'} · TLS verify: ${settings.verify_tls ? 'On' : 'Off'}</div>`
          : 'No Workbench base URL saved yet.';
        byId('api-key-hint').textContent = settings.has_api_key
          ? `A Workbench API key is already stored for this OWUI user (${settings.api_key_hint || 'hidden'}). Leave the field blank unless you want to rotate it.`
          : 'No Workbench API key is currently stored for this OWUI user.';
      };

      const fillFormFromContext = () => {
        const settings = (state.context || {}).settings || {};
        byId('base_url').value = settings.base_url || '';
        byId('api_key').value = '';
        byId('verify_tls').checked = !!settings.verify_tls;
      };

      const updateSummaryCards = () => {
        const summary = state.branchSummary;
        byId('summary-status').textContent = summary?.status || 'Pick a branch to inspect.';
        byId('summary-revision').textContent = summary?.latest_revision || '-';
        byId('summary-model-count').textContent = summary?.model_count ?? '-';
        byId('summary-element-count').textContent = summary?.element_count ?? '-';
        byId('summary-updated').textContent = formatDate(summary?.updated_at);
      };

      const updateManifestArea = () => {
        const manifestHost = byId('manifest-scopes');
        manifestHost.innerHTML = '';
        const manifest = state.manifest;
        byId('workbench-user-card').innerHTML = manifest
          ? `<div>${optionLabel(manifest.preferred_username, 'Unknown')}</div><div style="color:var(--text-muted);font-weight:500;">Token source: ${optionLabel(manifest.source, 'unknown')}</div>`
          : 'Not connected yet.';

        for (const scope of manifest?.scopes || []) {
          const pill = document.createElement('div');
          pill.className = 'pill';
          pill.textContent = scope;
          manifestHost.appendChild(pill);
        }
      };

      const updateOwuiSelectors = () => {
        const settings = (state.context || {}).settings || {};
        populateSelect(
          'owui_model_id',
          state.models,
          settings.owui_model_id,
          'Choose an OWUI model',
          (entry) => ({
            value: entry.id,
            label: entry.name || entry.id,
          }),
        );
        populateSelect(
          'owui_function_id',
          state.functions,
          settings.owui_function_id,
          'Optional: no function / agent override',
          (entry) => ({
            value: entry.id,
            label: [entry.name || entry.id, entry.type || 'function'].filter(Boolean).join(' · '),
          }),
        );
      };

      const currentProject = () => {
        const projectId = byId('workbench_project_id').value;
        return state.projects.find((entry) => entry.project_id === projectId) || null;
      };

      const updateProjectAndBranchSelectors = () => {
        const settings = (state.context || {}).settings || {};
        populateSelect(
          'workbench_project_id',
          state.projects,
          byId('workbench_project_id').value || settings.workbench_project_id,
          'Choose a Workbench project',
          (entry) => ({
            value: entry.project_id,
            label: entry.project_name || entry.project_id,
          }),
        );

        const project = currentProject();
        populateSelect(
          'workbench_branch_id',
          project?.branches || [],
          byId('workbench_branch_id').value || settings.workbench_branch_id,
          'Choose a cached branch',
          (entry) => ({
            value: entry.branch_id,
            label: entry.branch_name || entry.branch_id,
          }),
        );
      };

      const updateServerSelector = () => {
        const settings = (state.context || {}).settings || {};
        populateSelect(
          'workbench_server_id',
          state.servers,
          byId('workbench_server_id').value || settings.workbench_server_id,
          'Choose a Workbench server',
          (entry) => ({
            value: entry.server_id,
            label: `${entry.server_name || entry.server_id} (${entry.project_count} projects / ${entry.branch_count} branches)`,
          }),
        );
      };

      const updateBranchModelSelector = () => {
        const settings = (state.context || {}).settings || {};
        populateSelect(
          'workbench_branch_model_id',
          state.branchModels,
          settings.workbench_branch_model_id,
          'Optional: entire branch context',
          (entry) => ({
            value: entry.model_id || entry.id || '',
            label: entry.model_name || entry.name || entry.model_id || entry.id || 'Unnamed model',
          }),
        );
      };

      const readFormPayload = () => ({
        base_url: byId('base_url').value.trim(),
        api_key: byId('api_key').value.trim(),
        verify_tls: byId('verify_tls').checked,
        owui_model_id: byId('owui_model_id').value,
        owui_function_id: byId('owui_function_id').value,
        workbench_server_id: byId('workbench_server_id').value,
        workbench_project_id: byId('workbench_project_id').value,
        workbench_branch_id: byId('workbench_branch_id').value,
        workbench_branch_model_id: byId('workbench_branch_model_id').value,
      });

      const disableWorkbenchSelectors = (disabled) => {
        [
          'workbench_server_id',
          'workbench_project_id',
          'workbench_branch_id',
          'workbench_branch_model_id',
        ].forEach((id) => {
          byId(id).disabled = disabled;
        });
      };

      const loadOwuiOptions = async () => {
        const [modelsPayload, functionsPayload] = await Promise.all([
          fetchJson(boot.endpoints.owui_models),
          fetchJson(boot.endpoints.owui_functions),
        ]);
        state.models = Array.isArray(modelsPayload?.data) ? modelsPayload.data : [];
        state.functions = Array.isArray(functionsPayload) ? functionsPayload : [];
        updateOwuiSelectors();
      };

      const loadManifestAndServers = async () => {
        const contextSettings = (state.context || {}).settings || {};
        if (!contextSettings.base_url || !contextSettings.has_api_key) {
          state.manifest = null;
          state.servers = [];
          state.projects = [];
          state.branchModels = [];
          state.branchSummary = null;
          updateManifestArea();
          updateServerSelector();
          updateProjectAndBranchSelectors();
          updateBranchModelSelector();
          updateSummaryCards();
          disableWorkbenchSelectors(true);
          return;
        }

        disableWorkbenchSelectors(false);
        state.manifest = await fetchJson(boot.endpoints.manifest);
        state.servers = await fetchJson(boot.endpoints.servers);
        updateManifestArea();
        updateServerSelector();
      };

      const loadProjectsForSelectedServer = async (preferredProjectId = '') => {
        const serverId = byId('workbench_server_id').value;
        state.projects = [];
        state.branchModels = [];
        state.branchSummary = null;
        updateProjectAndBranchSelectors();
        updateBranchModelSelector();
        updateSummaryCards();

        if (!serverId) {
          return;
        }

        const payload = await fetchJson(`${boot.endpoints.projects}?server_id=${encodeURIComponent(serverId)}`);
        state.projects = Array.isArray(payload) ? payload : [];
        updateProjectAndBranchSelectors();

        if (preferredProjectId && state.projects.some((entry) => entry.project_id === preferredProjectId)) {
          byId('workbench_project_id').value = preferredProjectId;
          updateProjectAndBranchSelectors();
        }
      };

      const loadBranchDetails = async (preferredBranchModelId = '') => {
        const serverId = byId('workbench_server_id').value;
        const projectId = byId('workbench_project_id').value;
        const branchId = byId('workbench_branch_id').value;
        state.branchModels = [];
        state.branchSummary = null;
        updateBranchModelSelector();
        updateSummaryCards();

        if (!serverId || !projectId || !branchId) {
          return;
        }

        const summaryUrl = `${boot.endpoints.branch_summary}?server_id=${encodeURIComponent(serverId)}&project_id=${encodeURIComponent(projectId)}&branch_id=${encodeURIComponent(branchId)}`;
        const modelsUrl = `${boot.endpoints.branch_models}?server_id=${encodeURIComponent(serverId)}&project_id=${encodeURIComponent(projectId)}&branch_id=${encodeURIComponent(branchId)}`;
        const [summaryPayload, modelsPayload] = await Promise.all([
          fetchJson(summaryUrl),
          fetchJson(modelsUrl),
        ]);
        state.branchSummary = summaryPayload || null;
        state.branchModels = Array.isArray(modelsPayload) ? modelsPayload : [];
        updateSummaryCards();
        updateBranchModelSelector();

        if (
          preferredBranchModelId &&
          state.branchModels.some((entry) => (entry.model_id || entry.id || '') === preferredBranchModelId)
        ) {
          byId('workbench_branch_model_id').value = preferredBranchModelId;
        }
      };

      const reloadAll = async () => {
        setStatus('Loading OWUI and Workbench options...');
        state.context = await fetchJson(boot.endpoints.context);
        fillFormFromContext();
        updateMetaCards();
        await loadOwuiOptions();
        await loadManifestAndServers();

        const settings = (state.context || {}).settings || {};
        if (settings.workbench_server_id) {
          byId('workbench_server_id').value = settings.workbench_server_id;
          await loadProjectsForSelectedServer(settings.workbench_project_id);
        }
        if (settings.workbench_project_id) {
          byId('workbench_project_id').value = settings.workbench_project_id;
          updateProjectAndBranchSelectors();
        }
        if (settings.workbench_branch_id) {
          byId('workbench_branch_id').value = settings.workbench_branch_id;
          await loadBranchDetails(settings.workbench_branch_model_id);
        }

        setStatus('Workbench bridge ready.');
      };

      byId('workbench_server_id').addEventListener('change', async () => {
        try {
          setStatus('Loading projects for the selected Workbench server...');
          await loadProjectsForSelectedServer();
          setStatus('Project list updated.');
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      byId('workbench_project_id').addEventListener('change', async () => {
        try {
          updateProjectAndBranchSelectors();
          await loadBranchDetails();
          setStatus('Branch list updated.');
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      byId('workbench_branch_id').addEventListener('change', async () => {
        try {
          setStatus('Loading branch summary and cached branch models...');
          await loadBranchDetails();
          setStatus('Branch details loaded.');
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      byId('reload-button').addEventListener('click', async () => {
        try {
          await reloadAll();
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      byId('clear-button').addEventListener('click', async () => {
        try {
          setStatus('Clearing the saved Workbench bridge...');
          await fetchJson(boot.endpoints.context, { method: 'DELETE' });
          await reloadAll();
          setStatus('Saved Workbench bridge cleared.', 'success');
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      byId('workbench-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
          setStatus('Saving the Workbench bridge...');
          state.context = await fetchJson(boot.endpoints.context, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(readFormPayload()),
          });
          fillFormFromContext();
          updateMetaCards();
          await loadManifestAndServers();
          if (state.context.settings.workbench_server_id) {
            byId('workbench_server_id').value = state.context.settings.workbench_server_id;
            await loadProjectsForSelectedServer(state.context.settings.workbench_project_id);
          }
          if (state.context.settings.workbench_branch_id) {
            byId('workbench_branch_id').value = state.context.settings.workbench_branch_id;
            await loadBranchDetails(state.context.settings.workbench_branch_model_id);
          }
          setStatus('Workbench bridge saved for this OWUI user.', 'success');
        } catch (error) {
          setStatus(error.message || String(error), 'error');
        }
      });

      fillFormFromContext();
      updateMetaCards();
      updateSummaryCards();
      updateManifestArea();
      disableWorkbenchSelectors(true);
      reloadAll().catch((error) => {
        setStatus(error.message || String(error), 'error');
      });
    })();
  </script>
</body>
</html>
"""
        .replace('__BOOTSTRAP__', bootstrap)
    )


@router.get('/workbench/context')
async def get_workbench_context(user=Depends(get_verified_user)):
    return _build_workbench_context_payload(user)


@router.post('/workbench/context')
async def update_workbench_context(
    form_data: WorkbenchConfigForm,
    user=Depends(get_verified_user),
    db: AsyncSession = Depends(get_async_session),
):
    normalized_base_url = _normalize_workbench_base_url(form_data.base_url)
    if normalized_base_url and not normalized_base_url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Workbench base URL must start with http:// or https://.')

    existing = _get_workbench_user_settings(user)
    merged_settings = _merge_workbench_ui_settings(
        user,
        {
            'base_url': normalized_base_url,
            'api_key': _normalize_optional_text(form_data.api_key) or existing['api_key'],
            'verify_tls': bool(form_data.verify_tls),
            'owui_model_id': _normalize_optional_text(form_data.owui_model_id),
            'owui_function_id': _normalize_optional_text(form_data.owui_function_id),
            'workbench_server_id': _normalize_optional_text(form_data.workbench_server_id),
            'workbench_project_id': _normalize_optional_text(form_data.workbench_project_id),
            'workbench_branch_id': _normalize_optional_text(form_data.workbench_branch_id),
            'workbench_branch_model_id': _normalize_optional_text(form_data.workbench_branch_model_id),
            'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        },
    )

    updated_user = await Users.update_user_settings_by_id(user.id, merged_settings, db=db)
    if updated_user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.USER_NOT_FOUND)

    return _build_workbench_context_payload(updated_user)


@router.delete('/workbench/context')
async def clear_workbench_context(user=Depends(get_verified_user), db: AsyncSession = Depends(get_async_session)):
    merged_settings = dict(user.settings or {})
    merged_ui_settings = dict(merged_settings.get('ui') or {})
    merged_ui_settings.pop(TWC_WORKBENCH_SETTINGS_KEY, None)
    merged_settings['ui'] = merged_ui_settings

    updated_user = await Users.update_user_settings_by_id(user.id, merged_settings, db=db)
    if updated_user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ERROR_MESSAGES.USER_NOT_FOUND)

    return _build_workbench_context_payload(updated_user)


@router.get('/workbench/ui', response_class=HTMLResponse)
async def get_workbench_ui(user=Depends(get_verified_user)):
    return HTMLResponse(_build_workbench_ui_html(user))


@router.get('/workbench/cache/manifest')
async def get_workbench_cache_manifest(user=Depends(get_verified_user)):
    return await _workbench_api_json(user, 'GET', '/api/cache')


@router.get('/workbench/cache/servers')
async def get_workbench_cache_servers(user=Depends(get_verified_user)):
    return await _workbench_api_json(user, 'GET', '/api/cache/servers')


@router.get('/workbench/cache/projects')
async def get_workbench_cache_projects(server_id: str, user=Depends(get_verified_user)):
    normalized_server_id = _normalize_optional_text(server_id)
    if not normalized_server_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Workbench server id is required.')

    return await _workbench_api_json(
        user,
        'GET',
        f"/api/cache/servers/{_quote_workbench_path(normalized_server_id)}/projects",
    )


@router.get('/workbench/cache/branch/summary')
async def get_workbench_branch_summary(
    server_id: str,
    project_id: str,
    branch_id: str,
    user=Depends(get_verified_user),
):
    normalized_server_id = _normalize_optional_text(server_id)
    normalized_project_id = _normalize_optional_text(project_id)
    normalized_branch_id = _normalize_optional_text(branch_id)
    if not normalized_server_id or not normalized_project_id or not normalized_branch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Workbench server, project, and branch selections are required.',
        )

    return await _workbench_api_json(
        user,
        'GET',
        f"/api/cache/servers/{_quote_workbench_path(normalized_server_id)}/projects/{_quote_workbench_path(normalized_project_id)}/branches/{_quote_workbench_path(normalized_branch_id)}/summary",
    )


@router.get('/workbench/cache/branch/models')
async def get_workbench_branch_models(
    server_id: str,
    project_id: str,
    branch_id: str,
    user=Depends(get_verified_user),
):
    normalized_server_id = _normalize_optional_text(server_id)
    normalized_project_id = _normalize_optional_text(project_id)
    normalized_branch_id = _normalize_optional_text(branch_id)
    if not normalized_server_id or not normalized_project_id or not normalized_branch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Workbench server, project, and branch selections are required.',
        )

    return await _workbench_api_json(
        user,
        'GET',
        f"/api/cache/servers/{_quote_workbench_path(normalized_server_id)}/projects/{_quote_workbench_path(normalized_project_id)}/branches/{_quote_workbench_path(normalized_branch_id)}/models",
    )


class LdapServerConfig(BaseModel):
    label: str
    host: str
    port: int | None = None
    attribute_for_mail: str = 'mail'
    attribute_for_username: str = 'uid'
    app_dn: str
    app_dn_password: str
    search_base: str
    search_filters: str = ''
    use_tls: bool = True
    certificate_path: str | None = None
    validate_cert: bool = True
    ciphers: str | None = 'ALL'


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
    enable_ldap: bool | None = None


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
