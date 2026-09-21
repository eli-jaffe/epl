<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { askAgent } from '$lib/api';
	import { clearToken, getToken } from '$lib/auth';

	type Turn = { question: string; answer: string; error?: boolean };

	let ready = $state(false);
	let token = $state('');
	let query = $state('');
	let asking = $state(false);
	let turns: Turn[] = $state([]);

	onMount(() => {
		const stored = getToken();
		if (!stored) {
			goto('/login');
			return;
		}
		token = stored;
		ready = true;
	});

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		const question = query.trim();
		if (!question || asking) return;
		query = '';
		asking = true;
		try {
			const answer = await askAgent(question, token);
			turns = [...turns, { question, answer }];
		} catch (err) {
			const message = err instanceof Error ? err.message : 'Something went wrong.';
			turns = [...turns, { question, answer: message, error: true }];
		} finally {
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
			<button type="button" class="link" onclick={logout}>Log out</button>
		</header>

		<div class="turns">
			{#each turns as turn (turn.question + turn.answer)}
				<div class="turn">
					<p class="question">{turn.question}</p>
					<p class="answer" class:error={turn.error}>{turn.answer}</p>
				</div>
			{/each}
			{#if asking}
				<p class="pending">Thinking...</p>
			{/if}
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
