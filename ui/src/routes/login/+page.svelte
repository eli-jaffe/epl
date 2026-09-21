<script lang="ts">
	import { goto } from '$app/navigation';
	import { login, register } from '$lib/api';
	import { setToken } from '$lib/auth';

	let mode: 'login' | 'register' = $state('login');
	let email = $state('');
	let password = $state('');
	let error: string | null = $state(null);
	let busy = $state(false);

	async function submit(event: SubmitEvent) {
		event.preventDefault();
		error = null;
		busy = true;
		try {
			if (mode === 'register') {
				await register(email, password);
			}
			const token = await login(email, password);
			setToken(token);
			await goto('/');
		} catch (err) {
			error = err instanceof Error ? err.message : 'Something went wrong.';
		} finally {
			busy = false;
		}
	}
</script>

<main>
	<h1>EPL Fantasy Agent</h1>
	<form onsubmit={submit}>
		<label>
			Email
			<input type="email" bind:value={email} required autocomplete="email" />
		</label>
		<label>
			Password
			<input type="password" bind:value={password} required autocomplete="current-password" />
		</label>
		{#if error}
			<p class="error">{error}</p>
		{/if}
		<button type="submit" disabled={busy}>
			{busy ? 'Please wait...' : mode === 'login' ? 'Log in' : 'Register & log in'}
		</button>
	</form>
	<button type="button" class="link" onclick={() => (mode = mode === 'login' ? 'register' : 'login')}>
		{mode === 'login' ? "Don't have an account? Register" : 'Already have an account? Log in'}
	</button>
</main>

<style>
	main {
		max-width: 24rem;
		margin: 4rem auto;
		font-family: system-ui, sans-serif;
	}
	form {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		font-size: 0.9rem;
	}
	input {
		padding: 0.5rem;
		font-size: 1rem;
	}
	button {
		padding: 0.5rem;
		font-size: 1rem;
	}
	.error {
		color: #b00020;
		margin: 0;
	}
	.link {
		background: none;
		border: none;
		color: #0060df;
		cursor: pointer;
		margin-top: 1rem;
		padding: 0;
	}
</style>
