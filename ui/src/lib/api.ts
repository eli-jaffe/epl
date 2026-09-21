// Thin fetch wrappers around agent/main.py's routes. Calls go to relative
// paths (/auth/..., /chat) so vite.config.ts's dev proxy can forward them
// to the FastAPI backend without any CORS setup.

async function readErrorDetail(response: Response): Promise<string> {
	try {
		const body = await response.json();
		return typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
	} catch {
		return response.statusText;
	}
}

export async function login(email: string, password: string): Promise<string> {
	// fastapi-users' /auth/jwt/login expects OAuth2PasswordRequestForm --
	// form-urlencoded with a "username" field, not "email" -- confirmed by
	// reading fastapi_users.router.auth.get_auth_router's source.
	const body = new URLSearchParams({ username: email, password });
	const response = await fetch('/auth/jwt/login', {
		method: 'POST',
		headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
		body
	});
	if (!response.ok) {
		throw new Error(await readErrorDetail(response));
	}
	const data = await response.json();
	return data.access_token as string;
}

export async function register(email: string, password: string): Promise<void> {
	const response = await fetch('/auth/register', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ email, password })
	});
	if (!response.ok) {
		throw new Error(await readErrorDetail(response));
	}
}

export async function askAgent(query: string, token: string): Promise<string> {
	const response = await fetch('/chat', {
		method: 'POST',
		headers: {
			'Content-Type': 'application/json',
			Authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ query })
	});
	if (!response.ok) {
		throw new Error(await readErrorDetail(response));
	}
	const data = await response.json();
	return data.answer as string;
}
