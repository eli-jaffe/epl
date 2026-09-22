<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { getAdminQueryDetail, getCurrentUser, type QueryDetail, type Step } from '$lib/api';
	import { getToken } from '$lib/auth';

	const PHASE_COLORS: Record<string, string> = {
		reason: '#4C6EF5',
		plan: '#7950F2',
		execute: '#12B886',
		reflect: '#F59F00',
		synthesize: '#E64980'
	};

	let ready = $state(false);
	let authorized = $state(false);
	let token = $state('');
	let loading = $state(false);
	let error: string | null = $state(null);
	let detail: QueryDetail | null = $state(null);
	let expandedStepId: string | null = $state(null);
	let pollHandle: ReturnType<typeof setInterval> | null = null;

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
			// Poll while the query is still running, so the waterfall fills in
			// as the agent moves through steps instead of only updating on the
			// next manual visit to this page. Stops on its own once the query
			// resolves (success/failed) -- no reason to keep polling a finished
			// query -- and on unmount if you navigate away first.
			pollHandle = setInterval(async () => {
				if (detail?.status === 'running') {
					await load();
				} else if (pollHandle) {
					clearInterval(pollHandle);
					pollHandle = null;
				}
			}, 1000);
		}
	});

	onDestroy(() => {
		if (pollHandle) clearInterval(pollHandle);
	});

	async function load() {
		const queryId = page.params.id;
		if (!queryId) {
			error = 'No query id in URL.';
			return;
		}
		loading = true;
		error = null;
		try {
			detail = await getAdminQueryDetail(token, queryId);
		} catch (err) {
			error = err instanceof Error ? err.message : 'Something went wrong.';
		} finally {
			loading = false;
		}
	}

	function toggle(stepId: string) {
		expandedStepId = expandedStepId === stepId ? null : stepId;
	}

	function totalDurationMs(d: QueryDetail): number {
		if (d.duration_ms !== null) return d.duration_ms;
		// Query still running: fall back to the last known step boundary.
		const last = d.steps.at(-1);
		if (!last) return 1;
		const end = new Date(last.finished_at ?? last.started_at).getTime();
		return Math.max(1, end - new Date(d.started_at).getTime());
	}

	function barStyle(d: QueryDetail, step: Step): string {
		const queryStart = new Date(d.started_at).getTime();
		const total = totalDurationMs(d);
		const offset = new Date(step.started_at).getTime() - queryStart;
		const width = step.duration_ms ?? 0;
		const leftPct = Math.max(0, (offset / total) * 100);
		const widthPct = Math.max(0.3, (width / total) * 100);
		const color = PHASE_COLORS[step.phase] ?? '#888';
		return `left: ${leftPct}%; width: ${widthPct}%; background: ${color};`;
	}

	function formatDuration(ms: number | null): string {
		if (ms === null) return 'n/a';
		if (ms < 1000) return `${ms}ms`;
		return `${(ms / 1000).toFixed(2)}s`;
	}

	function formatStarted(iso: string): string {
		return new Date(iso).toLocaleString();
	}
</script>

{#if ready}
	<main>
		<header>
			<a href="/admin">&larr; All queries</a>
		</header>

		{#if !authorized}
			<p class="not-authorized">Not authorized. This page is for admins only.</p>
		{:else if loading}
			<p>Loading...</p>
		{:else if error}
			<p class="error">{error}</p>
		{:else if detail}
			<section class="summary">
				<h1>Query detail</h1>
				<dl>
					<dt>Asker</dt>
					<dd>{detail.user_email}</dd>
					<dt>Question</dt>
					<dd>{detail.query_text}</dd>
					<dt>Status</dt>
					<dd><span class="status status-{detail.status}">{detail.status}</span></dd>
					<dt>Started</dt>
					<dd>{formatStarted(detail.started_at)}</dd>
					<dt>Total duration</dt>
					<dd>{formatDuration(detail.duration_ms)}</dd>
					<dt>Final answer</dt>
					<dd class="final-answer">{detail.final_answer ?? '(none yet)'}</dd>
				</dl>
			</section>

			<section class="waterfall">
				<h2>Step timeline</h2>
				<div class="legend">
					{#each Object.entries(PHASE_COLORS) as [phase, color] (phase)}
						<span class="legend-item"><span class="swatch" style={`background:${color}`}></span>{phase}</span>
					{/each}
				</div>
				<div class="rows">
					{#each detail.steps as step (step.step_id)}
						<div class="row">
							<div class="row-label">
								{step.step_index}. {step.tool_name ?? step.phase}
							</div>
							<div class="track">
								<button
									type="button"
									class="bar"
									style={barStyle(detail, step)}
									onclick={() => toggle(step.step_id)}
									title={`${step.phase}${step.tool_name ? ' / ' + step.tool_name : ''} -- ${formatDuration(step.duration_ms)}`}
								></button>
							</div>
							<div class="row-duration">{formatDuration(step.duration_ms)}</div>
						</div>
						{#if expandedStepId === step.step_id}
							<div class="expanded">
								{#if step.summary}
									<p><strong>Summary:</strong> {step.summary}</p>
								{/if}
								{#if step.tool_args}
									<p><strong>Tool args:</strong></p>
									<pre>{JSON.stringify(step.tool_args, null, 2)}</pre>
								{/if}
								{#if step.tool_result}
									<p><strong>Tool result:</strong></p>
									<pre>{step.tool_result}</pre>
								{/if}
								{#if step.tool_error}
									<p class="error"><strong>Tool error:</strong> {step.tool_error}</p>
								{/if}
								{#if step.llm_calls.length > 0}
									<p><strong>LLM calls:</strong></p>
									<div class="llm-chips">
										{#each step.llm_calls as call (call.call_id)}
											<span class="chip">
												{call.model} -- in {call.input_tokens ?? '?'} / out {call.output_tokens ?? '?'}
												{#if call.cache_read_tokens}
													/ cache-read {call.cache_read_tokens}{/if}
											</span>
										{/each}
									</div>
								{/if}
							</div>
						{/if}
					{/each}
				</div>
			</section>
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
		gap: 1.5rem;
		padding: 0 1rem;
	}
	header a {
		color: #0060df;
		font-size: 0.9rem;
		text-decoration: none;
	}
	header a:hover {
		text-decoration: underline;
	}
	.not-authorized {
		color: #666;
	}
	.error {
		color: #b00020;
	}
	dl {
		display: grid;
		grid-template-columns: 8rem 1fr;
		gap: 0.4rem 1rem;
		font-size: 0.9rem;
	}
	dt {
		font-weight: 600;
		color: #444;
	}
	dd {
		margin: 0;
	}
	.final-answer {
		white-space: pre-wrap;
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
	.legend {
		display: flex;
		gap: 1rem;
		font-size: 0.8rem;
		color: #444;
		margin-bottom: 0.75rem;
	}
	.legend-item {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
	}
	.swatch {
		width: 0.7rem;
		height: 0.7rem;
		border-radius: 0.15rem;
		display: inline-block;
	}
	.rows {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.row {
		display: grid;
		grid-template-columns: 12rem 1fr 4rem;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.8rem;
	}
	.row-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: #444;
	}
	.track {
		position: relative;
		height: 1.4rem;
		background: #f2f2f2;
		border-radius: 0.2rem;
	}
	.bar {
		position: absolute;
		top: 0;
		height: 100%;
		border: none;
		border-radius: 0.2rem;
		cursor: pointer;
		min-width: 2px;
	}
	.bar:hover {
		filter: brightness(0.9);
	}
	.row-duration {
		text-align: right;
		color: #666;
	}
	.expanded {
		margin: 0.25rem 0 0.75rem 12.5rem;
		padding: 0.6rem 0.8rem;
		background: #fafafa;
		border: 1px solid #e0e0e0;
		border-radius: 0.3rem;
		font-size: 0.8rem;
	}
	.expanded p {
		margin: 0.4rem 0;
	}
	.expanded pre {
		white-space: pre-wrap;
		word-break: break-word;
		background: #fff;
		border: 1px solid #e0e0e0;
		border-radius: 0.25rem;
		padding: 0.5rem;
		font-size: 0.75rem;
		max-height: 16rem;
		overflow-y: auto;
	}
	.llm-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
	}
	.chip {
		background: #eef2ff;
		color: #333;
		padding: 0.2rem 0.5rem;
		border-radius: 1rem;
		font-size: 0.75rem;
	}
</style>
