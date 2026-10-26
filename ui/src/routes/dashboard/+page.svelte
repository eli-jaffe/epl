<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		getDashboardFilters,
		getMetric,
		getTopPlayers,
		type DashboardFilters,
		type Metric,
		type TopPlayers
	} from '$lib/api';
	import { getToken } from '$lib/auth';
	import TopPlayersChart from '$lib/components/TopPlayersChart.svelte';
	import MetricInfo from '$lib/components/MetricInfo.svelte';

	type Scope = 'season' | 'gameweek';
	type Panel = { data: TopPlayers | null; error: string; loading: boolean; infoOpen: boolean };

	let ready = $state(false);
	let token = '';
	let filters: DashboardFilters | null = $state(null);
	// Global filters: one pair of values drives both charts.
	let team = $state('');
	let position = $state('');
	let panels: Record<Scope, Panel> = $state({
		season: { data: null, error: '', loading: true, infoOpen: false },
		gameweek: { data: null, error: '', loading: true, infoOpen: false }
	});
	let metrics: Record<string, Metric> = $state({});

	onMount(async () => {
		const stored = getToken();
		if (!stored) {
			goto('/login');
			return;
		}
		token = stored;
		ready = true;
		try {
			filters = await getDashboardFilters(stored);
		} catch (e) {
			panels.season.error = panels.gameweek.error = (e as Error).message;
		}
	});

	// Refetch both charts whenever a filter changes (and once on load).
	$effect(() => {
		if (!ready) return;
		const t = team;
		const p = position;
		for (const scope of ['season', 'gameweek'] as Scope[]) {
			load(scope, t, p);
		}
	});

	async function load(scope: Scope, t: string, p: string) {
		const panel = panels[scope];
		panel.loading = true;
		panel.error = '';
		try {
			const data = await getTopPlayers(token, scope, t, p);
			// Drop a response that a newer filter change has already superseded.
			if (t !== team || p !== position) return;
			panel.data = data;
			panel.loading = false;
		} catch (e) {
			if (t !== team || p !== position) return;
			panel.error = (e as Error).message;
			panel.loading = false;
		}
	}

	async function toggleInfo(scope: Scope) {
		const panel = panels[scope];
		panel.infoOpen = !panel.infoOpen;
		const id = panel.data?.metric_id;
		if (panel.infoOpen && id && !metrics[id]) {
			try {
				metrics[id] = await getMetric(token, id);
			} catch (e) {
				panel.error = (e as Error).message;
			}
		}
	}

	function title(scope: Scope, data: TopPlayers | null): string {
		if (!data) return scope === 'season' ? 'Top scorers this season' : 'Top scorers this gameweek';
		return scope === 'season'
			? `Top scorers, ${data.season_id} season`
			: `Top scorers, gameweek ${data.gameweek}`;
	}
</script>

<svelte:head><title>Dashboard</title></svelte:head>

{#if ready}
	<main class="viz-root">
		<header>
			<h1>Dashboard</h1>
			<a href="/">← Back to chat</a>
		</header>

		<div class="filters">
			<label>
				Team
				<select bind:value={team} disabled={!filters}>
					<option value="">All teams</option>
					{#each filters?.teams ?? [] as t (t.short_name)}
						<option value={t.short_name}>{t.name}</option>
					{/each}
				</select>
			</label>
			<label>
				Position
				<select bind:value={position} disabled={!filters}>
					<option value="">All positions</option>
					{#each filters?.positions ?? [] as p (p)}
						<option value={p}>{p}</option>
					{/each}
				</select>
			</label>
		</div>

		{#each ['season', 'gameweek'] as const as scope (scope)}
			{@const panel = panels[scope]}
			<section class="card">
				<div class="card-head">
					<h2>{title(scope, panel.data)}</h2>
					<button
						type="button"
						class="info-btn"
						aria-label="About this chart"
						aria-expanded={panel.infoOpen}
						onclick={() => toggleInfo(scope)}>ⓘ</button
					>
				</div>
				{#if panel.infoOpen && panel.data && metrics[panel.data.metric_id]}
					<MetricInfo metric={metrics[panel.data.metric_id]} />
				{/if}
				{#if panel.error}
					<p class="error">{panel.error}</p>
				{:else if panel.data}
					<div class:stale={panel.loading}>
						<TopPlayersChart players={panel.data.players} {scope} />
					</div>
				{:else}
					<p class="muted">Loading…</p>
				{/if}
			</section>
		{/each}
	</main>
{/if}

<style>
	.viz-root {
		color-scheme: light;
		--surface-1: #fcfcfb;
		--surface-2: #f3f2ef;
		--text-primary: #0b0b0b;
		--text-secondary: #52514e;
		--grid: #e3e2de;
		--series-1: #2a78d6;
	}
	@media (prefers-color-scheme: dark) {
		.viz-root {
			color-scheme: dark;
			--surface-1: #1a1a19;
			--surface-2: #242423;
			--text-primary: #f0efec;
			--text-secondary: #c3c2b7;
			--grid: #3a3a38;
			--series-1: #3987e5;
		}
	}
	main {
		max-width: 52rem;
		margin: 0 auto;
		padding: 2rem 1rem;
		font-family: system-ui, sans-serif;
		background: var(--surface-1);
		color: var(--text-primary);
		min-height: 100vh;
		box-sizing: border-box;
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
	a {
		color: var(--text-secondary);
	}
	.filters {
		display: flex;
		gap: 1rem;
		flex-wrap: wrap;
		margin-bottom: 1.25rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: 0.8rem;
		color: var(--text-secondary);
	}
	select {
		font-size: 0.95rem;
		padding: 0.3rem 0.5rem;
	}
	.card {
		margin-bottom: 1.5rem;
		padding: 1rem;
		border: 1px solid var(--grid);
		border-radius: 0.5rem;
	}
	.card-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.5rem;
	}
	h2 {
		margin: 0;
		font-size: 1.05rem;
	}
	.info-btn {
		background: none;
		border: none;
		cursor: pointer;
		font-size: 1.1rem;
		color: var(--text-secondary);
		padding: 0.25rem 0.4rem;
	}
	.stale {
		opacity: 0.5;
	}
	.error {
		color: #c62828;
	}
	.muted {
		color: var(--text-secondary);
	}
</style>
