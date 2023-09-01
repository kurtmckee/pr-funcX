from __future__ import annotations

import logging

import globus_sdk
from globus_compute_sdk.sdk.login_manager.protocol import LoginManagerProtocol
from globus_compute_sdk.sdk.web_client import WebClient
from globus_sdk.scopes import AuthScopes

from .manager import ComputeScopeBuilder

log = logging.getLogger(__name__)

ComputeScopes = ComputeScopeBuilder()


class AuthorizerLoginManager(LoginManagerProtocol):
    """
    Implements a LoginManager that can be instantiated with authorizers.
    This manager can be used to create an Executor with authorizers created
    from previously acquired tokens, rather than requiring a Native App login
    flow or Client credentials.
    """

    def __init__(self, authorizers: dict[str, globus_sdk.RefreshTokenAuthorizer]):
        self.authorizers = authorizers

    def get_auth_client(self) -> globus_sdk.AuthClient:
        return globus_sdk.AuthClient(authorizer=self.authorizers[AuthScopes.openid])

    def get_web_client(
        self, *, base_url: str | None = None, app_name: str | None = None
    ) -> WebClient:
        return WebClient(
            base_url=base_url,
            app_name=app_name,
            authorizer=self.authorizers[ComputeScopes.resource_server],
        )

    def ensure_logged_in(self):
        return True

    def logout(self):
        log.warning("Logout cannot be invoked from a TokenLoginManager.")
