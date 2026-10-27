<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		askForChartStreaming,
		getDashboardFilters,
		getMetric,
		getTopPlayers,
		type DashboardFilters,
		type GeneratedChart as GeneratedChartData,
		type Metric,
		type TopPlayers
	} from '$lib/api';
	import { getToken } from '$lib/auth';
	import TopPlayersChart from '$lib/components/TopPlayersChart.svelte';
	import GeneratedChart from '$lib/components/GeneratedChart.svelte';
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

	// Agent-generated charts, newest first. `status` is the live progress line
	// while the agent works and is cleared when it finishes.
	type Generated = {
		prompt: string;
		status?: string;
		answer: string;
		chart: GeneratedChartData | null;
		error: boolean;
		infoOpen: boolean;
		sqlOpen: boolean;
	};
	let generated: Generated[] = $state([]);
	let chartPrompt = $state('');
	let building = $state(false);
	// One id per page load groups this session's prompts for admin troubleshooting.
	const conversationId = crypto.randomUUID();

	async function submitChartPrompt(event: SubmitEvent) {
		event.preventDefault();
		const prompt = chartPrompt.trim();
		if (!prompt || building) return;
		chartPrompt = '';
		building = true;
		generated.unshift({
			prompt,
			status: 'Starting...',
			answer: '',
			chart: null,
			error: false,
			infoOpen: false,
			sqlOpen: false
		});
		const item = generated[0];
		try {
			const result = await askForChartStreaming(prompt, token, conversationId, (message) => {
				item.status = message;
			});
			item.answer = result.answer;
			item.chart = result.chart;
			// The explanation is the point of the feature; show it by default.
			item.infoOpen = result.chart !== null;
		} catch (e) {
			item.answer = e instanceof Error ? e.message : 'Something went wrong.';
			item.error = true;
		} finally {
			item.status = undefined;
			building = false;
		}
	}

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

		<section class="ask">
			<h2>Ask for a chart</h2>
			<p class="muted">
				Describe the chart you want. The agent reuses an existing metric when one fits, or
				defines a new one and tells you how it's calculated. The filters above apply to the
				fixed charts only.
			</p>
			<form onsubmit={submitChartPrompt}>
				<input
					type="text"
					bind:value={chartPrompt}
					placeholder="e.g. Top 10 defenders by points per million this season"
					disabled={building}
				/>
				<button type="submit" disabled={building || !chartPrompt.trim()}>Create chart</button>
			</form>

			{#each generated as item (item)}
				<div class="card generated">
					<p class="prompt">{item.prompt}</p>
					{#if item.status}
						<p class="muted">{item.status}</p>
					{:else}
						{#if item.chart}
							<div class="card-head">
								<h2>{item.chart.title}</h2>
								<button
									type="button"
									class="info-btn"
									aria-label="About this chart"
									aria-expanded={item.infoOpen}
									onclick={() => (item.infoOpen = !item.infoOpen)}>ⓘ</button
								>
							</div>
							{#if item.infoOpen}
								{#each item.chart.metrics as metric (metric.id)}
									<MetricInfo {metric} />
								{/each}
							{/if}
							<GeneratedChart spec={item.chart.spec} />
							<button type="button" class="link" onclick={() => (item.sqlOpen = !item.sqlOpen)}>
								{item.sqlOpen ? 'Hide' : 'Show'} SQL
							</button>
							{#if item.sqlOpen}
								<pre>{item.chart.sql}</pre>
							{/if}
						{/if}
						<p class="answer" class:error={item.error}>{item.answer}</p>
					{/if}
				</div>
			{/each}
		</section>
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
		/* Categorical slots 1-8, fixed order (reference palette) -- used by
		   GeneratedChart for agent charts that color by a field. */
		--cat-1: #2a78d6;
		--cat-2: #eb6834;
		--cat-3: #1baf7a;
		--cat-4: #eda100;
		--cat-5: #e87ba4;
		--cat-6: #008300;
		--cat-7: #6250d6;
		--cat-8: #e34948;
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
			--cat-1: #3987e5;
			--cat-2: #d95926;
			--cat-3: #199e70;
			--cat-4: #c98500;
			--cat-5: #d55181;
			--cat-6: #008300;
			--cat-7: #9085e9;
			--cat-8: #e66767;
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
	.ask form {
		display: flex;
		gap: 0.5rem;
		margin-bottom: 1rem;
	}
	.ask input {
		flex: 1;
		font-size: 0.95rem;
		padding: 0.4rem 0.6rem;
	}
	.generated {
		margin-top: 1rem;
	}
	.prompt {
		margin: 0 0 0.5rem;
		font-weight: 600;
	}
	.answer {
		white-space: pre-wrap;
		margin: 0.75rem 0 0;
	}
	.link {
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		color: var(--text-secondary);
		text-decoration: underline;
		font-size: 0.85rem;
	}
	pre {
		overflow-x: auto;
		background: var(--surface-2);
		border-radius: 0.375rem;
		padding: 0.75rem;
		font-size: 0.8rem;
	}
</style>
