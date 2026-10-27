<script lang="ts">
	import type { Metric } from '$lib/api';

	let { metric }: { metric: Metric } = $props();
</script>

<div class="info" role="region" aria-label="About {metric.name}">
	<h3>{metric.name}</h3>
	{#if metric.status !== 'curated'}
		<span class="badge">AI-defined, not yet reviewed</span>
	{/if}
	<p>{metric.definition}</p>
	<dl>
		<dt>How it's calculated</dt>
		<dd>{metric.formula}</dd>
		<dt>Each row is</dt>
		<dd>{metric.grain}</dd>
		<dt>Source</dt>
		<dd>{metric.source_views.join(', ')}</dd>
		{#if metric.sql_expression}
			<dt>As SQL</dt>
			<dd><code>{metric.sql_expression}</code></dd>
		{/if}
	</dl>
	{#if metric.caveats.length}
		<ul>
			{#each metric.caveats as caveat}
				<li>{caveat}</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.info {
		background: var(--surface-2);
		border: 1px solid var(--grid);
		border-radius: 0.5rem;
		padding: 0.75rem 1rem;
		margin-bottom: 0.75rem;
		font-size: 0.9rem;
		color: var(--text-primary);
	}
	h3 {
		margin: 0 0 0.25rem;
		font-size: 0.95rem;
	}
	p {
		margin: 0.25rem 0 0.5rem;
	}
	dt {
		font-weight: 600;
		color: var(--text-secondary);
		font-size: 0.8rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	dd {
		margin: 0 0 0.5rem;
	}
	ul {
		margin: 0;
		padding-left: 1.1rem;
		color: var(--text-secondary);
	}
	.badge {
		display: inline-block;
		font-size: 0.75rem;
		padding: 0.1rem 0.5rem;
		border-radius: 0.25rem;
		background: #ffe8cc;
		color: #a04a00;
	}
</style>
