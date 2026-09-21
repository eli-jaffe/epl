// JWT storage -- localStorage, not a cookie, since the backend's
// fastapi-users setup uses BearerTransport (see agent/auth/users.py),
// which expects the token in an Authorization header, not a cookie jar.
const TOKEN_KEY = 'epl_auth_token';

export function getToken(): string | null {
	return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
	localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
	localStorage.removeItem(TOKEN_KEY);
}
