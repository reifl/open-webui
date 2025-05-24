<script lang="ts">
	import { decode } from 'html-entities';
	import { v4 as uuidv4 } from 'uuid';
	import { WEBUI_BASE_URL } from '$lib/constants';
	import { dev } from '$app/environment';

	import { getContext } from 'svelte';
	const i18n = getContext('i18n');

	import dayjs from '$lib/dayjs';
	import duration from 'dayjs/plugin/duration';
	import relativeTime from 'dayjs/plugin/relativeTime';

	dayjs.extend(duration);
	dayjs.extend(relativeTime);

	async function loadLocale(locales) {
		for (const locale of locales) {
			try {
				dayjs.locale(locale);
				break; // Stop after successfully loading the first available locale
			} catch (error) {
				console.error(`Could not load locale '${locale}':`, error);
			}
		}
	}

	// Assuming $i18n.languages is an array of language codes
	$: loadLocale($i18n.languages);

	import { slide } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';

	import ChevronUp from '../icons/ChevronUp.svelte';
	import ChevronDown from '../icons/ChevronDown.svelte';
	import Spinner from './Spinner.svelte';
	import CodeBlock from '../chat/Messages/CodeBlock.svelte';
	import Markdown from '../chat/Messages/Markdown.svelte';
	import Image from './Image.svelte';

	export let open = false;

	export let className = '';
	export let buttonClassName =
		'w-fit text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition';

	export let id = '';
	export let title = null;
	export let attributes = null;

	export let chevron = false;
	export let grow = false;

	export let disabled = false;
	export let hide = false;

	export let onChange: Function = () => {};

	$: onChange(open);

	const collapsibleId = uuidv4();

	function parseJSONString(str) {
		try {
			return parseJSONString(JSON.parse(str));
		} catch (e) {
			return str;
		}
	}

	function formatJSONString(str) {
		try {
			const parsed = parseJSONString(str);
			// If parsed is an object/array, then it's valid JSON
			if (typeof parsed === 'object') {
				return JSON.stringify(parsed, null, 2);
			} else {
				// It's a primitive value like a number, boolean, etc.
				return `${JSON.stringify(String(parsed))}`;
			}
		} catch (e) {
			// Not valid JSON, return as-is
			return str;
		}
	}

	// Enhanced file handling functions
	function isDataUrl(url) {
		return url.startsWith('data:');
	}

	function isAbsoluteUrl(url) {
		return url.startsWith('http://') || url.startsWith('https://');
	}

	function getDataUrlMimeType(dataUrl) {
		const match = dataUrl.match(/^data:([^;]+)/);
		return match ? match[1] : '';
	}

	async function getContentTypeFromServer(url) {
		try {
			const fullUrl = getFullUrl(url);
			// Use GET method but abort early to get headers without downloading the body
			const controller = new AbortController();
			const response = await fetch(fullUrl, { 
				method: 'GET',
				// Only include credentials in development (cross-origin), use default in production (same-origin)
				...(dev && { credentials: 'include' }),
				signal: controller.signal 
			});
			
			// Abort the request immediately after getting headers to prevent downloading the body
			controller.abort();
			
			return response.headers.get('content-type') || '';
		} catch (error) {
			// AbortError is expected, we only care about other errors
			if (error.name !== 'AbortError') {
				console.error('Failed to fetch content-type for:', url, error);
			}
			return '';
		}
	}

	function getFullUrl(url) {
		if (isDataUrl(url) || isAbsoluteUrl(url)) {
			return url;
		}
		// Handle relative URLs by prepending WEBUI_BASE_URL
		return `${WEBUI_BASE_URL}${url.startsWith('/') ? url : '/' + url}`;
	}

	async function determineFileType(file) {
		// Priority 1: Data URL with explicit MIME type
		if (isDataUrl(file)) {
			return getDataUrlMimeType(file);
		}
		
		// Priority 2: Get content-type from server for URLs
		if (file && typeof file === 'string') {
			return await getContentTypeFromServer(file);
		}
		
		// Fallback: return empty string for unknown types
		return '';
	}

	// Reactive store to track file types
	let fileTypes = {};
	let loadingFileTypes = {};

	async function updateFileTypes(files) {
		if (typeof files === 'object' && files) {
			const newFileTypes = {};
			const newLoadingStates = {};
			
			for (let i = 0; i < files.length; i++) {
				const file = files[i];
				// Show loading for URL requests (not data URLs)
				if (!isDataUrl(file)) {
					newLoadingStates[i] = true;
					loadingFileTypes = {...loadingFileTypes, ...newLoadingStates};
				}
				newFileTypes[i] = await determineFileType(file);
				newLoadingStates[i] = false;
			}
			
			fileTypes = newFileTypes;
			loadingFileTypes = newLoadingStates;
		}
	}

	// Reactive statement to update file types when files change
	$: if (attributes?.done === 'true' && parseJSONString(decode(attributes?.files ?? ''))) {
		const files = parseJSONString(decode(attributes?.files ?? ''));
		updateFileTypes(files);
	}
</script>

<div {id} class={className}>
	{#if title !== null}
		<!-- svelte-ignore a11y-no-static-element-interactions -->
		<!-- svelte-ignore a11y-click-events-have-key-events -->
		<div
			class="{buttonClassName} cursor-pointer"
			on:pointerup={() => {
				if (!disabled) {
					open = !open;
				}
			}}
		>
			<div
				class=" w-full font-medium flex items-center justify-between gap-2 {attributes?.done &&
				attributes?.done !== 'true'
					? 'shimmer'
					: ''}
			"
			>
				{#if attributes?.done && attributes?.done !== 'true'}
					<div>
						<Spinner className="size-4" />
					</div>
				{/if}

				<div class="">
					{#if attributes?.type === 'reasoning'}
						{#if attributes?.done === 'true' && attributes?.duration}
							{#if attributes.duration < 60}
								{$i18n.t('Thought for {{DURATION}} seconds', {
									DURATION: attributes.duration
								})}
							{:else}
								{$i18n.t('Thought for {{DURATION}}', {
									DURATION: dayjs.duration(attributes.duration, 'seconds').humanize()
								})}
							{/if}
						{:else}
							{$i18n.t('Thinking...')}
						{/if}
					{:else if attributes?.type === 'code_interpreter'}
						{#if attributes?.done === 'true'}
							{$i18n.t('Analyzed')}
						{:else}
							{$i18n.t('Analyzing...')}
						{/if}
					{:else if attributes?.type === 'tool_calls'}
						{#if attributes?.done === 'true'}
							<Markdown
								id={`${collapsibleId}-tool-calls-${attributes?.id}`}
								content={$i18n.t('View Result from **{{NAME}}**', {
									NAME: attributes.name
								})}
							/>
						{:else}
							<Markdown
								id={`${collapsibleId}-tool-calls-${attributes?.id}-executing`}
								content={$i18n.t('Executing **{{NAME}}**...', {
									NAME: attributes.name
								})}
							/>
						{/if}
					{:else}
						{title}
					{/if}
				</div>

				<div class="flex self-center translate-y-[1px]">
					{#if open}
						<ChevronUp strokeWidth="3.5" className="size-3.5" />
					{:else}
						<ChevronDown strokeWidth="3.5" className="size-3.5" />
					{/if}
				</div>
			</div>
		</div>
	{:else}
		<!-- svelte-ignore a11y-no-static-element-interactions -->
		<!-- svelte-ignore a11y-click-events-have-key-events -->
		<div
			class="{buttonClassName} cursor-pointer"
			on:pointerup={() => {
				if (!disabled) {
					open = !open;
				}
			}}
		>
			<div>
				<div class="flex items-start justify-between">
					<slot />

					{#if chevron}
						<div class="flex self-start translate-y-1">
							{#if open}
								<ChevronUp strokeWidth="3.5" className="size-3.5" />
							{:else}
								<ChevronDown strokeWidth="3.5" className="size-3.5" />
							{/if}
						</div>
					{/if}
				</div>

				{#if grow}
					{#if open && !hide}
						<div
							transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}
							on:pointerup={(e) => {
								e.stopPropagation();
							}}
						>
							<slot name="content" />
						</div>
					{/if}
				{/if}
			</div>
		</div>
	{/if}

	{#if attributes?.type === 'tool_calls'}
		{@const args = decode(attributes?.arguments)}
		{@const result = decode(attributes?.result ?? '')}
		{@const files = parseJSONString(decode(attributes?.files ?? ''))}

		{#if !grow}
			{#if open && !hide}
				<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
					{#if attributes?.type === 'tool_calls'}
						{#if attributes?.done === 'true'}
							<Markdown
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result`}
								content={`> \`\`\`json
> ${formatJSONString(args)}
> ${formatJSONString(result)}
> \`\`\``}
							/>
						{:else}
							<Markdown
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result`}
								content={`> \`\`\`json
> ${formatJSONString(args)}
> \`\`\``}
							/>
						{/if}
					{:else}
						<slot name="content" />
					{/if}
				</div>
			{/if}

			{#if attributes?.done === 'true'}
				{#if typeof files === 'object'}
					{#each files ?? [] as file, idx}
						{@const contentType = fileTypes[idx] || ''}
						{@const isLoading = loadingFileTypes[idx] || false}
						{@const displayUrl = isDataUrl(file) ? file : getFullUrl(file)}
						
						{#if isLoading}
							<div class="flex items-center gap-2 p-2 text-gray-500">
								<Spinner className="size-4" />
								<span>Loading file...</span>
							</div>
						{:else if contentType.startsWith('image/')}
							<Image
								id={`${collapsibleId}-tool-calls-${attributes?.id}-result-${idx}`}
								src={displayUrl}
								alt="Image"
							/>
						{:else if contentType.startsWith('audio/')}
							<audio class="w-full" controls src={displayUrl}>
								<track kind="captions" />
							</audio>
						{:else if contentType.startsWith('video/')}
							<video class="w-full" controls src={displayUrl}>
								<track kind="captions" />
							</video>
						{:else if contentType.includes('pdf')}
							<iframe 
								src={displayUrl} 
								class="w-full h-96 border rounded" 
								title="PDF Document">
							</iframe>
						{:else if contentType.startsWith('text/') || contentType.includes('json') || contentType.includes('xml')}
							{#if isDataUrl(file)}
								<div class="p-4 bg-gray-50 dark:bg-gray-800 rounded border">
									<div class="text-sm text-gray-600 dark:text-gray-400 mb-2">Text Content ({contentType}):</div>
									<pre class="text-sm overflow-auto max-h-64">{atob(file.split(',')[1] || '')}</pre>
								</div>
							{:else}
								<a 
									href={displayUrl} 
									target="_blank" 
									rel="noopener noreferrer"
									class="inline-flex items-center gap-2 text-blue-600 hover:text-blue-800 underline p-2"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
									</svg>
									View Text File ({contentType})
								</a>
							{/if}
						{:else if file}
							{#if isDataUrl(file)}
								<div class="p-4 bg-gray-50 dark:bg-gray-800 rounded border">
									<div class="text-sm text-gray-600 dark:text-gray-400 mb-2">
										File Content ({contentType || 'Unknown type'}):
									</div>
									<div class="text-xs text-gray-500 font-mono break-all">
										{file.substring(0, 100)}...
									</div>
								</div>
							{:else}
								<a 
									href={displayUrl} 
									target="_blank" 
									rel="noopener noreferrer"
									class="inline-flex items-center gap-2 text-blue-600 hover:text-blue-800 underline p-2"
								>
									<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
									</svg>
									Download File ({contentType || 'Unknown type'})
								</a>
							{/if}
						{/if}
					{/each}
				{/if}
			{/if}
		{/if}
	{:else if !grow}
		{#if open && !hide}
			<div transition:slide={{ duration: 300, easing: quintOut, axis: 'y' }}>
				<slot name="content" />
			</div>
		{/if}
	{/if}
</div>