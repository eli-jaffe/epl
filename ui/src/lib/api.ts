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

export type CurrentUser = {
	id: string;
	email: string;
	is_active: boolean;
	is_superuser: boolean;
	is_verified: boolean;
};

export async function getCurrentUser(token: string): Promise<CurrentUser> {
	const response = await fetch('/users/me', {
		headers: { Authorization: `Bearer ${token}` }
	});
	if (!response.ok) {
		throw new Error(await readErrorDetail(response));
	}
	return (await response.json()) as CurrentUser;
}

export type QuerySummary = {
	query_id: string;
	user_email: string;
	query_text: string;
	status: string;
	started_at: string;
	duration_ms: number | null;
};

export type AdminQueryFilters = {
	limit?: number;
	offset?: number;
	status?: string;
	user_email?: string;
};

export async function listAdminQueries(
	token: string,
	filters: AdminQueryFilters = {}
): Promise<QuerySummary[]> {
	const params = new URLSearchParams();
	if (filters.limit !== undefined) params.set('limit', String(filters.limit));
	if (filters.offset !== undefined) params.set('offset', String(filters.offset));
	if (filters.status) params.set('status', filters.status);
	if (filters.user_email) params.set('user_email', filters.user_email);
	const qs = params.toString();
	const response = await fetch(`/admin/queries${qs ? `?${qs}` : ''}`, {
		headers: { Authorization: `Bearer ${token}` }
	});
	if (!response.ok) {
		throw new Error(await readErrorDetail(response));
	}
	return (await response.json()) as QuerySummary[];
}

export type LlmCall = {
	call_id: string;
	model: string;
	input_tokens: number | null;
	output_tokens: number | null;
	cache_read_tokens: number | null;
	cache_write_tokens: number | null;
};

export type Step = {
	step_id: string;
	phase: string;
	step_index: number;
	tool_name: string | null;
	tool_args: Record<string, unknown> | null;
	tool_result: string | null;
	tool_success: boolean | null;
	tool_error: string | null;
	summary: string | null;
	started_at: string;
	finished_at: string | null;
	duration_ms: number | null;
	llm_calls: LlmCall[];
};

export type QueryDetail = {
	query_id: string;
	user_email: string;
	query_text: string;
	status: string;
	final_answer: string | null;
	started_at: string;
	finished_at: string | null;
	duration_ms: number | null;
	steps: Step[];
};

export async function getAdminQueryDetail(token: string, queryId: string): Promise<QueryDetail> {
	const response = await fetch(`/admin/queries/${queryId}`, {
		headers: { Authorization: `Bearer ${token}` }
	});
	if (!response.ok) {
		throw new Error(await readErrorDetail(response));
	}
	return (await response.json()) as QueryDetail;
}
