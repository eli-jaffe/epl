<script lang="ts">
	import embed from 'vega-embed';

	let { spec }: { spec: Record<string, any> } = $props();

	let el: HTMLDivElement | undefined = $state();
	let renderError = $state('');
	// Bumped when the OS light/dark setting flips so the chart re-reads its
	// CSS color tokens -- Vega bakes colors into the spec, it can't follow CSS.
	let themeTick = $state(0);

	$effect(() => {
		const mq = matchMedia('(prefers-color-scheme: dark)');
		const bump = () => themeTick++;
		mq.addEventListener('change', bump);
		return () => mq.removeEventListener('change', bump);
	});

	// The agent is told to send only mark + encoding (see agent/loop.py's
	// CHART_INSTRUCTIONS); size and theme are applied here so every generated
	// chart matches the fixed ones. Our values win over anything it sent.
	function themed(base: Record<string, any>, token: (name: string) => string) {
		// A discrete y axis (horizontal bars) gets a row-per-category height,
		// anything else a fixed one -- `step` is only valid on discrete axes.
		const yType = base.encoding?.y?.type;
		const height = yType === 'nominal' || yType === 'ordinal' ? { step: 28 } : 320;
		const categorical = Array.from({ length: 8 }, (_, i) => token(`--cat-${i + 1}`));
		const text = token('--text-secondary');
		const axis = { labelColor: text, titleColor: text, gridColor: token('--grid'), domainColor: token('--grid'), tickColor: token('--grid') };
		return {
			...base,
			$schema: 'https://vega.github.io/schema/vega-lite/v6.json',
			width: 'container',
			height,
			background: null,
			config: {
				view: { stroke: null },
				range: { category: categorical },
				mark: { color: categorical[0], tooltip: true },
				bar: { cornerRadiusTopRight: 4, cornerRadiusBottomRight: 4 },
				axis,
				legend: { labelColor: text, titleColor: text },
				title: { color: token('--text-primary') }
			}
		};
	}

	$effect(() => {
		void themeTick;
		if (!el) return;
		const css = getComputedStyle(el);
		const token = (name: string) => css.getPropertyValue(name).trim();
		renderError = '';

		let result: Awaited<ReturnType<typeof embed>> | undefined;
		let cancelled = false;
		// `spec` arrives as a Svelte $state proxy (it lives on a reactive page
		// item), and vega calls structuredClone on it, which throws on a
		// Proxy ("could not be cloned"). Snapshot to a plain object first.
		embed(el, themed($state.snapshot(spec), token) as any, { actions: false, renderer: 'svg' })
			.then((r) => {
				if (cancelled) r.finalize();
				else result = r;
			})
			.catch((e) => {
				if (!cancelled) renderError = e instanceof Error ? e.message : String(e);
			});
		return () => {
			cancelled = true;
			result?.finalize();
		};
	});
</script>

{#if renderError}
	<p class="error">Couldn't draw this chart: {renderError}</p>
{/if}
<div bind:this={el} class="chart" class:hidden={!!renderError}></div>

<style>
	.chart {
		width: 100%;
	}
	.hidden {
		display: none;
	}
	.error {
		color: #c62828;
		font-size: 0.9rem;
	}
</style>
