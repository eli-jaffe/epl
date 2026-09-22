import adapter from '@sveltejs/adapter-auto';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [
		sveltekit({
			compilerOptions: {
				// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
				runes: ({ filename }) =>
					filename.split(/[/\\]/).includes('node_modules') ? undefined : true
			},

			// adapter-auto only supports some environments, see https://svelte.dev/docs/kit/adapter-auto for a list.
			// If your environment is not supported, or you settled on a specific environment, switch out the adapter.
			// See https://svelte.dev/docs/kit/adapters for more information about adapters.
			adapter: adapter()
		})
	],
	server: {
		// Dev-only: forward auth/chat/users calls to the FastAPI backend
		// (agent/main.py, uvicorn on :8000) as if same-origin, so the browser
		// never needs the backend's own CORS config. Production will need a
		// real reverse proxy or CORS setup once this stops being local-only.
		proxy: {
			'/auth': 'http://localhost:8000',
			'/chat': 'http://localhost:8000',
			'/users': 'http://localhost:8000',
			// Scoped to the backend's actual route prefix (/admin/queries...),
			// not bare /admin -- the frontend also owns /admin and /admin/[id]
			// as page routes (ui/src/routes/admin/), and a bare '/admin' proxy
			// key would shadow those, intercepting even a plain page load
			// before SvelteKit's router ever sees it.
			'/admin/queries': 'http://localhost:8000'
		}
	}
});
