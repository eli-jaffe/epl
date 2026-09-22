<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { askAgentStreaming, getChatHistory, getCurrentUser } from '$lib/api';
	import { clearToken, getToken } from '$lib/auth';

	type Turn = { question: string; answer: string; error?: boolean; status?: string };
	type HistoryTurn = Turn & { expanded: boolean };

	let ready = $state(false);
	let token = $state('');
	let query = $state('');
	let asking = $state(false);
	let turns: Turn[] = $state([]);
	let history: HistoryTurn[] = $state([]);
	let isSuperuser = $state(false);

	onMount(() => {
		const stored = getToken();
		if (!stored) {
			goto('/login');
			return;
		}
		token = stored;
		ready = true;
		// Best-effort: an Admin link is UX only, the API enforces the real
		// boundary -- so a failure here just means no link, not a crash.
		getCurrentUser(stored)
			.then((user) => {
				isSuperuser = user.is_superuser;
			})
			.catch(() => {});
		// Past chats render in their own collapsed-by-default "Previous chats"
		// section, separate from the live conversation below -- newest-first
		// (the endpoint's native order), each one collapsed until clicked so
		// the page opens showing just the input, not a wall of old answers.
		getChatHistory(stored)
			.then((entries) => {
				history = entries.map((entry) => ({
					question: entry.query_text,
					answer: entry.final_answer ?? '(no answer recorded)',
					error: entry.status === 'failed',
					expanded: false
				}));
			})
			.catch(() => {});
	});

	function toggleHistory(index: number) {
		history[index].expanded = !history[index].expanded;
	}

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		const question = query.trim();
		if (!question || asking) return;
		query = '';
		asking = true;
		turns = [...turns, { question, answer: '', status: 'Thinking...' }];
		// Mutate through turns[index], not a captured `turn` reference -- Svelte
		// 5's $state deep-reactivity proxies the array's contents at the time
		// `turns` is reassigned, so a plain object reference held from before
		// that assignment is NOT the same object as the reactive proxy Svelte
		// tracks; mutating it silently updates the underlying data but never
		// triggers a re-render. Found empirically: the UI froze on "Thinking..."
		// and never showed the final answer.
		const index = turns.length - 1;
		try {
			turns[index].answer = await askAgentStreaming(question, token, (msg) => {
				turns[index].status = msg;
			});
		} catch (err) {
			turns[index].answer = err instanceof Error ? err.message : 'Something went wrong.';
			turns[index].error = true;
		} finally {
			turns[index].status = undefined;
			asking = false;
		}
	}

	function logout() {
		clearToken();
		goto('/login');
	}
</script>

{#if ready}
	<main>
		<header>
			<h1>EPL Fantasy Agent</h1>
			<div class="header-actions">
				{#if isSuperuser}
					<a href="/admin">Admin</a>
				{/if}
				<button type="button" class="link" onclick={logout}>Log out</button>
			</div>
		</header>

		{#if history.length > 0}
			<section class="history">
				<h2>Previous chats</h2>
				{#each history as entry, i (i)}
					<div class="history-item">
						<button
							type="button"
							class="history-question"
							onclick={() => toggleHistory(i)}
							aria-expanded={entry.expanded}
						>
							<span class="chevron" class:open={entry.expanded}>&#9656;</span>
							{entry.question}
						</button>
						{#if entry.expanded}
							<p class="answer" class:error={entry.error}>{entry.answer}</p>
						{/if}
					</div>
				{/each}
			</section>
		{/if}

		<div class="turns">
			{#each turns as turn, i (i)}
				<div class="turn">
					<p class="question">{turn.question}</p>
					{#if turn.status}
						<p class="pending">{turn.status}</p>
					{:else}
						<p class="answer" class:error={turn.error}>{turn.answer}</p>
					{/if}
				</div>
			{/each}
		</div>

		<form onsubmit={submit}>
			<input
				type="text"
				bind:value={query}
				placeholder="Ask about players, fixtures, or fantasy strategy..."
				disabled={asking}
			/>
			<button type="submit" disabled={asking || !query.trim()}>Ask</button>
		</form>
	</main>
{/if}

<style>
	main {
		max-width: 40rem;
		margin: 2rem auto;
		font-family: system-ui, sans-serif;
		display: flex;
		flex-direction: column;
		gap: 1rem;
		min-height: calc(100vh - 4rem);
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}
	.header-actions {
		display: flex;
		align-items: center;
		gap: 1rem;
	}
	.header-actions a {
		color: #0060df;
		text-decoration: none;
		font-size: 0.9rem;
	}
	.header-actions a:hover {
		text-decoration: underline;
	}
	.history {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		border-bottom: 1px solid #e0e0e0;
		padding-bottom: 0.75rem;
	}
	.history h2 {
		font-size: 0.85rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: #888;
		margin: 0 0 0.25rem;
	}
	.history-item {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.history-question {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		background: none;
		border: none;
		text-align: left;
		font: inherit;
		font-weight: 600;
		color: inherit;
		cursor: pointer;
		padding: 0.3rem 0;
	}
	.chevron {
		display: inline-block;
		font-size: 0.75rem;
		color: #888;
		transition: transform 0.15s ease;
	}
	.chevron.open {
		transform: rotate(90deg);
	}
	.history-item .answer {
		margin: 0 0 0.4rem 1.1rem;
	}
	.turns {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
		overflow-y: auto;
	}
	.turn {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.question {
		font-weight: 600;
		margin: 0;
	}
	.answer {
		margin: 0;
		white-space: pre-wrap;
	}
	.answer.error {
		color: #b00020;
	}
	.pending {
		color: #666;
		font-style: italic;
	}
	form {
		display: flex;
		gap: 0.5rem;
	}
	input {
		flex: 1;
		padding: 0.6rem;
		font-size: 1rem;
	}
	button {
		padding: 0.6rem 1rem;
		font-size: 1rem;
	}
	.link {
		background: none;
		border: none;
		color: #0060df;
		cursor: pointer;
		padding: 0;
	}
</style>
