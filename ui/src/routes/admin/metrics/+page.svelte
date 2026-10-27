<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		getCurrentUser,
		listAdminMetrics,
		mergeMetric,
		promoteMetric,
		unmergeMetric,
		type AdminMetric
	} from '$lib/api';
	import { getToken } from '$lib/auth';
	import MetricInfo from '$lib/components/MetricInfo.svelte';

	let ready = $state(false);
	let authorized = $state(false);
	let token = '';
	let error = $state('');
	let metrics: AdminMetric[] = $state([]);
	let openId = $state('');
	// Per-row merge target picked in the dropdown, keyed by metric id.
	let mergeTarget: Record<string, string> = $state({});
	let busyId = $state('');
	// Snippet returned by the last promote, shown until dismissed.
	let promoted: { id: string; snippet: string } | null = $state(null);

	onMount(async () => {
		const stored = getToken();
		if (!stored) {
			goto('/login');
			return;
		}
		token = stored;
		try {
			authorized = (await getCurrentUser(token)).is_superuser;
		} catch {
			authorized = false;
		}
		ready = true;
		if (authorized) await load();
	});

	async function load() {
		try {
			metrics = await listAdminMetrics(token);
		} catch (e) {
			error = (e as Error).message;
		}
	}

	async function act(id: string, run: () => Promise<unknown>) {
		busyId = id;
		error = '';
		try {
			await run();
			await load();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busyId = '';
		}
	}

	const merge = (m: AdminMetric) => act(m.id, () => mergeMetric(token, m.id, mergeTarget[m.id]));
	const unmerge = (m: AdminMetric) => act(m.id, () => unmergeMetric(token, m.id));
	const promote = (m: AdminMetric) =>
		act(m.id, async () => {
			promoted = { id: m.id, snippet: (await promoteMetric(token, m.id)).snippet };
		});

	// Merge targets: anything that isn't merged itself and isn't this metric.
	const targetsFor = (m: AdminMetric) => metrics.filter((t) => t.id !== m.id && t.status !== 'merged');
	const label = (id: string) => metrics.find((m) => m.id === id)?.name ?? id;
</script>

<svelte:head><title>Admin: Metrics</title></svelte:head>

{#if ready}
	<main>
		<header>
			<h1>Admin: Metrics library</h1>
			<span><a href="/admin">Agent queries</a> · <a href="/dashboard">Dashboard</a></span>
		</header>

		{#if !authorized}
			<p>Not authorized. This page is for admins only.</p>
		{:else}
			<p class="muted">
				Requests count every chart that used a metric, including charts that used metrics merged into
				it. <strong>Merge</strong> folds a near-duplicate into another metric (undoable).
				<strong>Promote</strong> marks a metric for the curated tier and gives you the code to paste
				into <code>agent/metrics.py</code>.
			</p>
			{#if error}<p class="error">{error}</p>{/if}

			{#if promoted}
				<div class="snippet">
					<p>
						<strong>{label(promoted.id)}</strong> is marked promoted. Paste this into
						<code>_CURATED</code> in <code>agent/metrics.py</code> and commit it:
					</p>
					<pre>{promoted.snippet}</pre>
					<button type="button" onclick={() => (promoted = null)}>Dismiss</button>
				</div>
			{/if}

			<table>
				<thead>
					<tr><th>Metric</th><th>Status</th><th class="num">Requests</th><th>Defined / reused</th><th></th></tr>
				</thead>
				<tbody>
					{#each metrics as m (m.id)}
						<tr class:dim={m.status === 'merged'}>
							<td>
								<button type="button" class="link" onclick={() => (openId = openId === m.id ? '' : m.id)}>
									{m.name}
								</button>
								{#if m.merged_into}<span class="muted"> → {label(m.merged_into)}</span>{/if}
								{#if m.merged_from.length}
									<span class="muted"> (+ {m.merged_from.map(label).join(', ')})</span>
								{/if}
							</td>
							<td>{m.status}</td>
							<td class="num">{m.requests}</td>
							<td>{m.own.defined} / {m.own.reused}</td>
							<td class="actions">
								{#if m.status === 'proposed'}
									<select bind:value={mergeTarget[m.id]} aria-label="Merge {m.name} into">
										<option value={undefined}>Merge into…</option>
										{#each targetsFor(m) as t (t.id)}
											<option value={t.id}>{t.name}</option>
										{/each}
									</select>
									<button type="button" disabled={!mergeTarget[m.id] || busyId === m.id} onclick={() => merge(m)}>
										Merge
									</button>
									<button type="button" disabled={busyId === m.id} onclick={() => promote(m)}>Promote</button>
								{:else if m.status === 'merged'}
									<button type="button" disabled={busyId === m.id} onclick={() => unmerge(m)}>Undo merge</button>
								{:else if m.status === 'promoted'}
									<button type="button" onclick={() => (promoted = { id: m.id, snippet: m.snippet ?? '' })}>
										Show code
									</button>
								{/if}
							</td>
						</tr>
						{#if openId === m.id}
							<tr class="detail"><td colspan="5"><MetricInfo metric={m} /></td></tr>
						{/if}
					{/each}
				</tbody>
			</table>
		{/if}
	</main>
{/if}

<style>
	main {
		/* Tokens MetricInfo reads; the dashboard page defines them on its own root. */
		--surface-2: #f3f2ef;
		--text-primary: #0b0b0b;
		--text-secondary: #52514e;
		--grid: #e3e2de;
		max-width: 62rem;
		margin: 0 auto;
		padding: 2rem 1rem;
		font-family: system-ui, sans-serif;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
	}
	h1 {
		margin: 0 0 1rem;
		font-size: 1.4rem;
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
		border-bottom: 1px solid #ddd;
		vertical-align: top;
	}
	.num {
		text-align: right;
	}
	.actions {
		display: flex;
		gap: 0.4rem;
		flex-wrap: wrap;
	}
	.dim {
		opacity: 0.6;
	}
	.muted {
		color: #666;
	}
	.error {
		color: #c62828;
	}
	.link {
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		text-decoration: underline;
		font-size: inherit;
		color: inherit;
	}
	.snippet {
		border: 1px solid #ddd;
		border-radius: 0.5rem;
		padding: 0.75rem 1rem;
		margin-bottom: 1rem;
	}
	pre {
		overflow-x: auto;
		background: #f3f2ef;
		padding: 0.75rem;
		font-size: 0.8rem;
	}
</style>
