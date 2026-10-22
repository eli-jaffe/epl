<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import {
		cancelQuery,
		getCurrentUser,
		listAdminQueries,
		resumeQueryStreaming,
		type QuerySummary
	} from '$lib/api';
	import { getToken } from '$lib/auth';

	let ready = $state(false);
	let authorized = $state(false);
	let token = $state('');
	let loading = $state(false);
	let error: string | null = $state(null);
	let queries: QuerySummary[] = $state([]);
	let statusFilter = $state('');
	let userEmailFilter = $state('');
	let conversationFilter = $state('');
	let busyIds: string[] = $state([]);

	onMount(async () => {
		const stored = getToken();
		if (!stored) {
			goto('/login');
			return;
		}
		token = stored;
		conversationFilter = page.url.searchParams.get('conversation_id') ?? '';
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
				user_email: userEmailFilter.trim() || undefined,
				conversation_id: conversationFilter.trim() || undefined
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

	async function act(queryId: string, run: () => Promise<unknown>) {
		busyIds = [...busyIds, queryId];
		error = null;
		try {
			await run();
		} catch (err) {
			error = err instanceof Error ? err.message : 'Something went wrong.';
		} finally {
			busyIds = busyIds.filter((id) => id !== queryId);
			await load();
		}
	}

	function shortId(id: string | null): string {
		return id ? id.slice(0, 8) : '-';
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
					<input type="text" bind:value={statusFilter} placeholder="success, failed, running, interrupted" />
				</label>
				<label>
					User email
					<input type="text" bind:value={userEmailFilter} placeholder="filter by asker" />
				</label>
				<label>
					Conversation ID
					<input type="text" bind:value={conversationFilter} placeholder="full conversation id" />
				</label>
				<button type="submit" disabled={loading}>{loading ? 'Loading...' : 'Filter'}</button>
			</form>

			{#if error}
				<p class="error">{error}</p>
			{/if}

			<table>
				<thead>
					<tr>
						<th>Conversation</th>
						<th>Query ID</th>
						<th>Asker</th>
						<th>Query</th>
						<th>Status</th>
						<th>Started</th>
						<th>Duration</th>
						<th></th>
					</tr>
				</thead>
				<tbody>
					{#each queries as q (q.query_id)}
						<tr>
							<td class="mono" title={q.conversation_id ?? ''}>
								{#if q.conversation_id}
									<button
										type="button"
										class="link"
										onclick={() => ((conversationFilter = q.conversation_id ?? ''), load())}
										>{shortId(q.conversation_id)}</button
									>
								{:else}-{/if}
							</td>
							<td class="mono" title={q.query_id}>{shortId(q.query_id)}</td>
							<td><a href={`/admin/${q.query_id}`}>{q.user_email}</a></td>
							<td class="query-text"><a href={`/admin/${q.query_id}`}>{q.query_text}</a></td>
							<td><span class="status status-{q.status}">{q.status}</span></td>
							<td>{formatStarted(q.started_at)}</td>
							<td>{formatDuration(q.duration_ms)}</td>
							<td class="actions">
								{#if q.status === 'interrupted'}
									{#if busyIds.includes(q.query_id)}
										Working...
									{:else}
										<button
											type="button"
											onclick={() => act(q.query_id, () => resumeQueryStreaming(q.query_id, token, () => {}))}
											>Restart</button
										>
										<button type="button" onclick={() => act(q.query_id, () => cancelQuery(q.query_id, token))}
											>Cancel</button
										>
									{/if}
								{/if}
							</td>
						</tr>
					{:else}
						<tr>
							<td colspan="8" class="empty">No queries found.</td>
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
	.status-interrupted {
		background: #ffe8cc;
		color: #a04a00;
	}
	.status-running {
		background: #fff3cd;
		color: #856404;
	}
	.actions {
		white-space: nowrap;
		font-size: 0.8rem;
	}
	.mono {
		font-family: ui-monospace, monospace;
		font-size: 0.8rem;
	}
	.link {
		background: none;
		border: none;
		padding: 0;
		color: #0060df;
		font: inherit;
		cursor: pointer;
	}
	.link:hover {
		text-decoration: underline;
	}
	.empty {
		color: #666;
		text-align: center;
	}
</style>
