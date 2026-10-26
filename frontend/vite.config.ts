import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(() => {
	const codespaceName = process.env.CODESPACE_NAME;
	const portForwardingDomain = process.env.GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN;
	const codespacesHost =
		codespaceName && portForwardingDomain
			? `${codespaceName}-5173.${portForwardingDomain}`
			: undefined;
	const allowedHosts = ['.app.github.dev', '.github.dev', 'localhost', '127.0.0.1'];
	const proxyTarget = 'http://127.0.0.1:8000';

	return {
		plugins: [react],
		server: {
			host: '0.0.0.0',
			port: 5173,
			strictPort: true,
			allowedHosts,
			hmr: codespacesHost
				? {
						protocol: 'wss',
						host: codespacesHost,
						clientPort: 443,
					}
				: undefined,
			watch: {
				usePolling: true,
			},
			proxy: {
				'/api': {
					target: proxyTarget,
					changeOrigin: true,
					ws: true,
				},
				'/health': {
					target: proxyTarget,
					changeOrigin: true,
					rewrite: () => '/api/v1/health',
				},
				'/ready': {
					target: proxyTarget,
					changeOrigin: true,
					rewrite: () => '/api/v1/ready',
				},
			},
		},
		preview: {
			allowedHosts,
			proxy: {
				'/api': {
					target: proxyTarget,
					changeOrigin: true,
					ws: true,
				},
				'/health': {
					target: proxyTarget,
					changeOrigin: true,
					rewrite: () => '/api/v1/health',
				},
				'/ready': {
					target: proxyTarget,
					changeOrigin: true,
					rewrite: () => '/api/v1/ready',
				},
			},
		},
	};
});
