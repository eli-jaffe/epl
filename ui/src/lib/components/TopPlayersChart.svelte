<script lang="ts">
	import embed from 'vega-embed';
	import type { TopPlayer } from '$lib/api';

	let { players, scope }: { players: TopPlayer[]; scope: 'season' | 'gameweek' } = $props();

	let el: HTMLDivElement | undefined = $state();
	// Bumped when the OS light/dark setting flips so the chart re-reads its
	// CSS color tokens -- Vega bakes colors into the spec, it can't follow CSS.
	let themeTick = $state(0);

	$effect(() => {
		const mq = matchMedia('(prefers-color-scheme: dark)');
		const bump = () => themeTick++;
		mq.addEventListener('change', bump);
		return () => mq.removeEventListener('change', bump);
	});

	$effect(() => {
		void themeTick;
		if (!el || players.length === 0) return;
		const css = getComputedStyle(el);
		const token = (name: string) => css.getPropertyValue(name).trim();

		// "Name (TEAM)" keeps two same-named players on separate bars.
		const rows = players.map((p) => ({
			...p,
			label: `${p.web_name} (${p.team_short_name})`
		}));

		const tooltip = [
			{ field: 'web_name', title: 'Player' },
			{ field: 'team_short_name', title: 'Team' },
			{ field: 'position', title: 'Position' },
			{ field: 'total_points', title: 'Points' },
			{ field: 'minutes', title: 'Minutes' },
			...(scope === 'gameweek'
				? [
						{ field: 'goals_scored', title: 'Goals' },
						{ field: 'assists', title: 'Assists' },
						{ field: 'bonus', title: 'Bonus' },
						{ field: 'opponents', title: 'Opponent' }
					]
				: [])
		];

		const spec: any = {
			$schema: 'https://vega.github.io/schema/vega-lite/v6.json',
			width: 'container',
			height: { step: 28 },
			padding: { left: 4, right: 28, top: 4, bottom: 4 },
			background: null,
			data: { values: rows },
			encoding: {
				y: {
					field: 'label',
					type: 'nominal',
					sort: '-x',
					axis: {
						title: null,
						domain: false,
						ticks: false,
						labelColor: token('--text-secondary'),
						labelFontSize: 12,
						labelLimit: 160
					}
				},
				x: {
					field: 'total_points',
					type: 'quantitative',
					scale: { domainMin: 0 },
					axis: {
						title: null,
						domain: false,
						ticks: false,
						tickCount: 5,
						grid: true,
						gridColor: token('--grid'),
						labelColor: token('--text-secondary'),
						labelFontSize: 11
					}
				}
			},
			layer: [
				{
					mark: {
						type: 'bar',
						color: token('--series-1'),
						size: 18,
						cornerRadiusTopRight: 4,
						cornerRadiusBottomRight: 4
					},
					encoding: { tooltip }
				},
				{
					mark: { type: 'text', align: 'left', dx: 6, fontSize: 12, color: token('--text-primary') },
					encoding: { text: { field: 'total_points', type: 'quantitative' } }
				}
			],
			config: { view: { stroke: null } }
		};

		let result: Awaited<ReturnType<typeof embed>> | undefined;
		let cancelled = false;
		embed(el, spec, { actions: false, renderer: 'svg' }).then((r) => {
			if (cancelled) r.finalize();
			else result = r;
		});
		return () => {
			cancelled = true;
			result?.finalize();
		};
	});
</script>

{#if players.length === 0}
	<p class="empty">No players match these filters.</p>
{:else}
	<div bind:this={el} class="chart"></div>
{/if}

<style>
	.chart {
		width: 100%;
	}
	.empty {
		color: var(--text-secondary);
		margin: 1rem 0;
	}
</style>
