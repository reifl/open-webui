<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { WEBUI_BASE_URL } from '$lib/constants';

	export let url: string;

	let container: HTMLDivElement;
	let renderer: import('three').WebGLRenderer | null = null;
	let animationId: number;
	let loading = true;
	let error = '';
	let showGrid = true;
	let gridRef: import('three').GridHelper | null = null;

	function toggleGrid() {
		showGrid = !showGrid;
		if (gridRef) gridRef.visible = showGrid;
	}

	function downloadModel() {
		const fullUrl = url.startsWith('http') ? url : `${WEBUI_BASE_URL}${url}`;
		const a = document.createElement('a');
		a.href = fullUrl;
		a.download = 'model.glb';
		a.click();
	}

	onMount(async () => {
		try {
			const THREE = await import('three');
			const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');
			const { OrbitControls } = await import('three/examples/jsm/controls/OrbitControls.js');

			const width = container.clientWidth || 480;
			const height = Math.round(width * 0.5625); // 16:9

			// Scene
			const scene = new THREE.Scene();
			scene.background = new THREE.Color(0x1a1b2e);

			// Camera
			const camera = new THREE.PerspectiveCamera(60, width / height, 0.01, 1000);
			camera.position.set(0, 1, 4);

			// Renderer
			renderer = new THREE.WebGLRenderer({ antialias: true });
			renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
			renderer.setSize(width, height);
			renderer.shadowMap.enabled = true;
			container.appendChild(renderer.domElement);

			// Lighting
			const ambient = new THREE.AmbientLight(0xffffff, 0.7);
			scene.add(ambient);

			const key = new THREE.DirectionalLight(0xffffff, 1.2);
			key.position.set(5, 8, 5);
			key.castShadow = true;
			scene.add(key);

			const fill = new THREE.DirectionalLight(0x8888ff, 0.4);
			fill.position.set(-5, 2, -5);
			scene.add(fill);

			// Grid helper
			const grid = new THREE.GridHelper(10, 20, 0x333355, 0x222244);
			grid.visible = showGrid;
			gridRef = grid;
			scene.add(grid);

			// Controls
			const controls = new OrbitControls(camera, renderer.domElement);
			controls.enableDamping = true;
			controls.dampingFactor = 0.05;
			controls.minDistance = 0.5;
			controls.maxDistance = 50;
			controls.target.set(0, 0, 0);

			// Load GLB
			const fullUrl = url.startsWith('http') ? url : `${WEBUI_BASE_URL}${url}`;
			const loader = new GLTFLoader();
			loader.load(
				fullUrl,
				(gltf) => {
					const model = gltf.scene;

					// Auto-center + auto-scale
					const box = new THREE.Box3().setFromObject(model);
					const center = box.getCenter(new THREE.Vector3());
					const size = box.getSize(new THREE.Vector3());
					const maxDim = Math.max(size.x, size.y, size.z);

					if (maxDim > 0) {
						const scale = 3 / maxDim;
						model.scale.setScalar(scale);
						box.setFromObject(model);
						box.getCenter(center);
					}

					model.position.sub(center);
					model.position.y += box.getSize(new THREE.Vector3()).y / 2;
					scene.add(model);

					// Adjust camera
					const scaledSize = maxDim > 0 ? 3 : 2;
					camera.position.set(scaledSize * 1.2, scaledSize * 0.8, scaledSize * 1.5);
					controls.target.set(0, 0, 0);
					controls.update();

					loading = false;
				},
				undefined,
				(err) => {
					error = 'Failed to load 3D model';
					loading = false;
					console.error('GLTFLoader error:', err);
				}
			);

			// Resize observer
			const ro = new ResizeObserver(() => {
				const w = container.clientWidth || 480;
				const h = Math.round(w * 0.5625);
				camera.aspect = w / h;
				camera.updateProjectionMatrix();
				renderer?.setSize(w, h);
			});
			ro.observe(container);

			// Animation loop
			const animate = () => {
				animationId = requestAnimationFrame(animate);
				controls.update();
				renderer?.render(scene, camera);
			};
			animate();
		} catch (e) {
			error = 'Failed to initialize 3D viewer';
			loading = false;
			console.error(e);
		}
	});

	onDestroy(() => {
		if (animationId) cancelAnimationFrame(animationId);
		renderer?.dispose();
	});
</script>

<div class="w-full rounded-xl overflow-hidden border border-gray-700/50 bg-[#1a1b2e] my-2 relative">
	{#if loading && !error}
		<div class="absolute inset-0 flex items-center justify-center z-10">
			<div class="flex items-center gap-2 text-gray-400 text-sm">
				<svg class="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
					<circle
						class="opacity-25"
						cx="12"
						cy="12"
						r="10"
						stroke="currentColor"
						stroke-width="4"
					/>
					<path
						class="opacity-75"
						fill="currentColor"
						d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
					/>
				</svg>
				Loading 3D model…
			</div>
		</div>
	{/if}

	{#if error}
		<div class="flex items-center justify-center h-32 text-red-400 text-sm">{error}</div>
	{/if}

	<div bind:this={container} class="w-full"></div>

	{#if !loading && !error}
		<!-- Toolbar -->
		<div class="absolute top-2 right-2 flex items-center gap-1.5 z-10">
			<!-- Grid toggle -->
			<button
				on:click={toggleGrid}
				title={showGrid ? 'Grid ausblenden' : 'Grid einblenden'}
				class="flex items-center gap-1 px-2 py-1 rounded-lg text-xs transition
					   bg-black/40 hover:bg-black/60 backdrop-blur-sm
					   {showGrid ? 'text-blue-300' : 'text-gray-500'}"
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 20 20"
					fill="currentColor"
					class="size-3.5"
				>
					<path
						fill-rule="evenodd"
						d="M.99 5.24A2.25 2.25 0 0 1 3.25 3h13.5A2.25 2.25 0 0 1 19 5.25l.01 9.5A2.25 2.25 0 0 1 16.76 17H3.26A2.25 2.25 0 0 1 1 14.74l-.01-9.5Zm8.26 9.52v-.625a.75.75 0 0 0-.75-.75H3.25a.75.75 0 0 0-.75.75v.615c0 .414.336.75.75.75h5.373a.75.75 0 0 0 .627-.74Zm1.5 0a.75.75 0 0 0 .627.74h5.373a.75.75 0 0 0 .75-.75v-.615a.75.75 0 0 0-.75-.75H11.5a.75.75 0 0 0-.75.75v.625Zm6.75-3.63v-.625a.75.75 0 0 0-.75-.75H11.5a.75.75 0 0 0-.75.75v.625c0 .414.336.75.75.75h5.25a.75.75 0 0 0 .75-.75Zm-8.25 0v-.625a.75.75 0 0 0-.75-.75H3.25a.75.75 0 0 0-.75.75v.625c0 .414.336.75.75.75H8.5a.75.75 0 0 0 .75-.75ZM17.5 7.5v-.625a.75.75 0 0 0-.75-.75H11.5a.75.75 0 0 0-.75.75V7.5c0 .414.336.75.75.75h5.25a.75.75 0 0 0 .75-.75Zm-8.25 0v-.625a.75.75 0 0 0-.75-.75H3.25a.75.75 0 0 0-.75.75V7.5c0 .414.336.75.75.75H8.5a.75.75 0 0 0 .75-.75Z"
						clip-rule="evenodd"
					/>
				</svg>
				Grid
			</button>

			<!-- Download button -->
			<button
				on:click={downloadModel}
				title="GLB herunterladen"
				class="flex items-center gap-1 px-2 py-1 rounded-lg text-xs transition
					   bg-black/40 hover:bg-black/60 backdrop-blur-sm text-gray-300 hover:text-white"
			>
				<svg
					xmlns="http://www.w3.org/2000/svg"
					viewBox="0 0 20 20"
					fill="currentColor"
					class="size-3.5"
				>
					<path
						d="M10.75 2.75a.75.75 0 0 0-1.5 0v8.614L6.295 8.235a.75.75 0 1 0-1.09 1.03l4.25 4.5a.75.75 0 0 0 1.09 0l4.25-4.5a.75.75 0 0 0-1.09-1.03l-2.955 3.129V2.75Z"
					/>
					<path
						d="M3.5 12.75a.75.75 0 0 0-1.5 0v2.5A2.75 2.75 0 0 0 4.75 18h10.5A2.75 2.75 0 0 0 18 15.25v-2.5a.75.75 0 0 0-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5Z"
					/>
				</svg>
				Download
			</button>
		</div>

		<!-- Hint -->
		<div class="absolute bottom-2 right-3 text-gray-600 text-xs select-none pointer-events-none">
			drag · scroll · right-click to pan
		</div>
	{/if}
</div>
