"""Authentication helpers for API key and Cognis JWT validation."""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from jwt import InvalidTokenError, PyJWKClient, PyJWKSet

logger = logging.getLogger(__name__)

_EXPECTED_ISSUER = "cognis"
_EXPECTED_AUDIENCE = "intaris"
_JWKS_CACHE_TTL_SECONDS = 300
_JWKS_TIMEOUT_SECONDS = 5
# Minimum spacing between JWKS fetches, bounding refreshes triggered by
# unknown kids or tokens that fail validation with the cached key.
_JWKS_MIN_REFRESH_INTERVAL_SECONDS = 10
# After a failed fetch, do not contact the JWKS endpoint again for this long.
# Requests keep using the last known keys (bounded by the stale limit) or fail
# fast instead of each blocking on the fetch timeout.
_JWKS_FAILURE_BACKOFF_SECONDS = 30
# Maximum age of last known keys still accepted while the endpoint is failing.
_JWKS_MAX_STALE_SECONDS = 3600


class JWKSKeyCache:
    """Thread-safe JWKS key cache with single-flight refresh and failure backoff.

    Callers resolve keys synchronously; concurrent refreshes are serialized so
    an unavailable JWKS endpoint costs at most one fetch timeout per backoff
    window instead of one per request.
    """

    def __init__(
        self,
        jwks_url: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = PyJWKClient(
            jwks_url,
            cache_jwk_set=False,
            cache_keys=False,
            timeout=_JWKS_TIMEOUT_SECONDS,
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str | None, object] = {}
        self._fetched_at: float | None = None
        self._last_attempt_at: float | None = None
        self._retry_after = 0.0

    def get_key(self, kid: str | None, *, force_refresh: bool = False) -> object:
        """Return the verification key for ``kid``.

        Raises:
            InvalidTokenError: If no usable key can be resolved.
        """
        with self._lock:
            now = self._clock()
            fresh = (
                self._fetched_at is not None
                and now - self._fetched_at < _JWKS_CACHE_TTL_SECONDS
            )
            if force_refresh or not fresh or kid not in self._keys:
                self._maybe_refresh(now)
            key = self._keys.get(kid)
            if key is None:
                raise InvalidTokenError("Unable to resolve JWT signing key")
            if (
                self._fetched_at is None
                or now - self._fetched_at > _JWKS_MAX_STALE_SECONDS
            ):
                raise InvalidTokenError("JWKS keys are stale and cannot be refreshed")
            return key

    def _maybe_refresh(self, now: float) -> None:
        if now < self._retry_after:
            return
        if (
            self._last_attempt_at is not None
            and now - self._last_attempt_at < _JWKS_MIN_REFRESH_INTERVAL_SECONDS
        ):
            return
        self._last_attempt_at = now
        try:
            data = self._client.fetch_data()
            if not isinstance(data, dict):
                raise ValueError("JWKS endpoint did not return a JSON object")
            jwk_set = PyJWKSet.from_dict(data)
        except Exception as exc:
            self._retry_after = now + _JWKS_FAILURE_BACKOFF_SECONDS
            logger.warning(
                "JWKS refresh failed; backing off for %ss (%s)",
                _JWKS_FAILURE_BACKOFF_SECONDS,
                exc.__class__.__name__,
            )
            return
        keys: dict[str | None, object] = {k.key_id: k.key for k in jwk_set.keys}
        if len(jwk_set.keys) == 1:
            # Tokens without a kid header may use the only published key.
            keys.setdefault(None, jwk_set.keys[0].key)
        self._keys = keys
        self._fetched_at = self._clock()
        self._retry_after = 0.0


@dataclass(frozen=True)
class JWTAuthContext:
    """Validated Cognis JWT identity."""

    user_id: str
    agent_id: str | None = None
    controller_claims: dict | None = None


@dataclass(frozen=True)
class AuthResolution:
    """Resolved request identity."""

    user_id: str | None
    agent_id: str | None
    user_bound: bool
    method: str
    controller_claims: dict | None = None


class CognisJWTValidator:
    """Validate Cognis-issued service JWTs for Intaris."""

    def __init__(self, *, public_key_path: str = "", jwks_url: str = "") -> None:
        self._public_key_path = public_key_path
        self._jwks_url = jwks_url
        self._public_key: str | None = None
        self._jwks_cache: JWKSKeyCache | None = (
            JWKSKeyCache(jwks_url) if jwks_url and not public_key_path else None
        )

        if self._public_key_path:
            with open(self._public_key_path, encoding="utf-8") as f:
                self._public_key = f.read()

    def validate(
        self, token: str, header_agent_id: str | None = None
    ) -> JWTAuthContext:
        from intaris.sanitize import validate_agent_id

        claims = self._decode(token)
        user_id = claims.get("sub")
        if not isinstance(user_id, str) or not user_id.strip():
            raise InvalidTokenError("JWT is missing a valid sub claim")

        claim_agent_id = claims.get("agent_id")
        if claim_agent_id is not None:
            if not isinstance(claim_agent_id, str):
                raise InvalidTokenError(
                    "JWT agent_id claim must be a string when present"
                )
            claim_agent_id = validate_agent_id(claim_agent_id)

        if claim_agent_id and header_agent_id and claim_agent_id != header_agent_id:
            raise InvalidTokenError("X-Agent-Id does not match JWT agent_id claim")

        controller = None
        if claims.get("typ") == "controller":
            required = ("session_id", "agent_id", "jti", "iat", "nbf", "exp")
            if any(key not in claims for key in required):
                raise InvalidTokenError(
                    "Controller token missing binding/lifetime claims"
                )
            if not all(
                isinstance(claims[key], str) and claims[key]
                for key in ("session_id", "agent_id", "jti")
            ):
                raise InvalidTokenError("Invalid controller identity claims")
            if not all(
                type(claims[key]) in (int, float) for key in ("iat", "nbf", "exp")
            ):
                raise InvalidTokenError("Invalid controller lifetime")
            if not 0 < claims["exp"] - claims["iat"] <= 300:
                raise InvalidTokenError("Controller lifetime exceeds five minutes")
            if (
                not isinstance(claims.get("capabilities"), list)
                or "intaris:controller_context" not in claims["capabilities"]
            ):
                raise InvalidTokenError("Controller capability missing")
            controller = claims
        return JWTAuthContext(
            user_id=user_id,
            agent_id=claim_agent_id or header_agent_id,
            controller_claims=controller,
        )

    def decode_claims(self, token: str) -> dict:
        """Decode and validate a JWT, returning all claims.

        Unlike ``validate()``, this returns the raw claims dict including
        custom claims like ``typ``, ``target``, and ``jti``. Used by the
        exchange token endpoint which needs access to these claims.

        Raises:
            InvalidTokenError: If the JWT is invalid, expired, or has
                wrong issuer/audience.
        """
        return self._decode(token)

    def _decode(self, token: str) -> dict:
        if self._public_key is not None:
            return self._decode_with_key(token, self._public_key)

        if self._jwks_cache is None:
            raise InvalidTokenError("JWT validation is not configured")

        kid = jwt.get_unverified_header(token).get("kid")
        key = self._jwks_cache.get_key(kid)
        try:
            return self._decode_with_key(token, key)
        except InvalidTokenError:
            # The signing key may have rotated; retry once with a (rate
            # limited) refresh, and only if the resolved key changed.
            refreshed = self._jwks_cache.get_key(kid, force_refresh=True)
            if refreshed is key:
                raise
            logger.info("JWT validation failed with cached JWKS key; refreshed")
            return self._decode_with_key(token, refreshed)

    @staticmethod
    def _decode_with_key(token: str, key: object) -> dict:
        return jwt.decode(
            token,
            key,
            algorithms=["ES256"],
            issuer=_EXPECTED_ISSUER,
            audience=_EXPECTED_AUDIENCE,
        )


_validator: CognisJWTValidator | None = None
_validator_config: tuple[str, str] | None = None
_validator_lock = threading.Lock()


def get_jwt_validator(
    public_key_path: str = "",
    jwks_url: str = "",
) -> CognisJWTValidator | None:
    """Return a cached JWT validator for the current configuration."""
    global _validator, _validator_config

    # Auth runs in worker threads; serialize creation so concurrent cold-start
    # requests share one validator (and one JWKS cache/backoff state).
    with _validator_lock:
        if not public_key_path and not jwks_url:
            _validator = None
            _validator_config = None
            return None

        config_key = (public_key_path, jwks_url)
        if _validator is None or _validator_config != config_key:
            _validator = CognisJWTValidator(
                public_key_path=public_key_path,
                jwks_url=jwks_url,
            )
            _validator_config = config_key
        return _validator


def looks_like_jwt(token: str) -> bool:
    """Return True when the token has the shape of a JWT."""
    return token.count(".") == 2


def match_api_key(token: str, api_keys: dict[str, str]) -> str | None:
    """Match a token against the api_keys mapping using constant-time compare."""
    matched_user: str | None = None
    for key, user_id in api_keys.items():
        if hmac.compare_digest(token, key):
            matched_user = user_id
    return matched_user


def resolve_auth(
    *,
    token: str,
    header_user_id: str | None,
    header_agent_id: str | None,
    api_key: str,
    api_keys: dict[str, str],
    jwt_public_key: str,
    jwks_url: str,
    allow_no_auth: bool,
) -> AuthResolution | None:
    """Resolve request identity from JWT, API key, or no-auth headers."""
    validator = get_jwt_validator(jwt_public_key, jwks_url)

    if allow_no_auth and not token:
        logger.info("Auth resolved via no-auth mode")
        return AuthResolution(
            user_id=header_user_id,
            agent_id=header_agent_id,
            user_bound=False,
            method="noauth",
        )

    if token and validator is not None and looks_like_jwt(token):
        try:
            auth_ctx = validator.validate(token, header_agent_id=header_agent_id)
        except InvalidTokenError:
            logger.info("JWT auth rejected; falling back to API key auth")
        else:
            logger.info("Auth resolved via JWT for user=%s", auth_ctx.user_id)
            return AuthResolution(
                user_id=auth_ctx.user_id,
                agent_id=auth_ctx.agent_id,
                user_bound=True,
                method="jwt",
                controller_claims=auth_ctx.controller_claims,
            )

    mapped_user_id = match_api_key(token, api_keys)
    if mapped_user_id is not None:
        if mapped_user_id != "*":
            logger.debug("Auth resolved via mapped API key for user=%s", mapped_user_id)
            return AuthResolution(
                user_id=mapped_user_id,
                agent_id=header_agent_id,
                user_bound=True,
                method="api_key",
            )
        logger.debug("Auth resolved via wildcard API key")
        return AuthResolution(
            user_id=header_user_id,
            agent_id=header_agent_id,
            user_bound=False,
            method="api_key",
        )

    if api_key and hmac.compare_digest(token, api_key):
        logger.debug("Auth resolved via single API key")
        return AuthResolution(
            user_id=header_user_id,
            agent_id=header_agent_id,
            user_bound=False,
            method="api_key",
        )

    return None
