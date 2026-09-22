<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { getCurrentUser, listAdminQueries, type QuerySummary } from '$lib/api';
	import { getToken } from '$lib/auth';

	let ready = $state(false);
	let authorized = $state(false);
	let token = $state('');
	let loading = $state(false);
	let error: string | null = $state(null);
	let queries: QuerySummary[] = $state([]);
	let statusFilter = $state('');
	let userEmailFilter = $state('');

	onMount(async () => {
		const stored = getToken();
		if (!stored) {
			goto('/login');
			return;
		}
		token = stored;
		try {
			const user = await getCurrentUser(token);
			authorized = user.is_superuser;
		} catch {
			authorized = false;
		}
		ready = true;
		if (authorized) {
			await load();
		}
	});

	async function load() {
		loading = true;
		error = null;
		try {
			queries = await listAdminQueries(token, {
				status: statusFilter.trim() || undefined,
				user_email: userEmailFilter.trim() || undefined
			});
		} catch (err) {
			error = err instanceof Error ? err.message : 'Something went wrong.';
		} finally {
			loading = false;
		}
	}

	function formatDuration(ms: number | null): string {
		if (ms === null) return 'running...';
		if (ms < 1000) return `${ms}ms`;
		return `${(ms / 1000).toFixed(1)}s`;
	}

	function formatStarted(iso: string): string {
		return new Date(iso).toLocaleString();
	}
</script>

{#if ready}
	<main>
		<header>
			<h1>Admin: Agent Queries</h1>
			<a href="/">Back to chat</a>
		</header>

		{#if !authorized}
			<p class="not-authorized">Not authorized. This page is for admins only.</p>
		{:else}
			<form class="filters" onsubmit={(e) => (e.preventDefault(), load())}>
				<label>
					Status
					<input type="text" bind:value={statusFilter} placeholder="success, failed, running" />
				</label>
				<label>
					User email
					<input type="text" bind:value={userEmailFilter} placeholder="filter by asker" />
				</label>
				<button type="submit" disabled={loading}>{loading ? 'Loading...' : 'Filter'}</button>
			</form>

			{#if error}
				<p class="error">{error}</p>
			{/if}

			<table>
				<thead>
					<tr>
						<th>Asker</th>
						<th>Query</th>
						<th>Status</th>
						<th>Started</th>
						<th>Duration</th>
					</tr>
				</thead>
				<tbody>
					{#each queries as q (q.query_id)}
						<tr>
							<td><a href={`/admin/${q.query_id}`}>{q.user_email}</a></td>
							<td class="query-text"><a href={`/admin/${q.query_id}`}>{q.query_text}</a></td>
							<td><span class="status status-{q.status}">{q.status}</span></td>
							<td>{formatStarted(q.started_at)}</td>
							<td>{formatDuration(q.duration_ms)}</td>
						</tr>
					{:else}
						<tr>
							<td colspan="5" class="empty">No queries found.</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	</main>
{/if}

<style>
	main {
		max-width: 60rem;
		margin: 2rem auto;
		font-family: system-ui, sans-serif;
		display: flex;
		flex-direction: column;
		gap: 1rem;
		padding: 0 1rem;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}
	header a {
		color: #0060df;
		font-size: 0.9rem;
	}
	.not-authorized {
		color: #666;
	}
	.filters {
		display: flex;
		gap: 1rem;
		align-items: flex-end;
	}
	.filters label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.85rem;
	}
	.filters input {
		padding: 0.4rem;
		font-size: 0.9rem;
	}
	.filters button {
		padding: 0.45rem 0.9rem;
		font-size: 0.9rem;
	}
	.error {
		color: #b00020;
	}
	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.9rem;
	}
	th,
	td {
		text-align: left;
		padding: 0.5rem;
		border-bottom: 1px solid #e0e0e0;
	}
	.query-text {
		max-width: 24rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	td a,
	th {
		color: inherit;
		text-decoration: none;
	}
	td a:hover {
		text-decoration: underline;
	}
	.status {
		padding: 0.15rem 0.5rem;
		border-radius: 0.25rem;
		font-size: 0.8rem;
	}
	.status-success {
		background: #e3f8e8;
		color: #1a7f37;
	}
	.status-failed {
		background: #fbe3e3;
		color: #b00020;
	}
	.status-running {
		background: #fff3cd;
		color: #856404;
	}
	.empty {
		color: #666;
		text-align: center;
	}
</style>
