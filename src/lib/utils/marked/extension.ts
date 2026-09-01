// Helper function to find matching closing tag
function findMatchingClosingTag(src: string, openTag: string, closeTag: string): number {
	let depth = 1;
	let index = openTag.length;
	while (depth > 0 && index < src.length) {
		if (src.startsWith(openTag, index)) {
			depth++;
		} else if (src.startsWith(closeTag, index)) {
			depth--;
		}
		if (depth > 0) {
			index++;
		}
	}
	return depth === 0 ? index + closeTag.length : -1;
}

// Function to parse attributes from tag
function parseAttributes(tag: string): { [key: string]: string } {
	const attributes: { [key: string]: string } = {};
	const attrRegex = /(\w+)="(.*?)"/g;
	let match;
	while ((match = attrRegex.exec(tag)) !== null) {
		attributes[match[1]] = match[2];
	}
	return attributes;
}

function detailsTokenizer(src: string) {
	// Updated regex to capture attributes inside <details>
	const detailsRegex = /^<details(\s+[^>]*)?>\n/;
	const summaryRegex = /^<summary>(.*?)<\/summary>\n/;

	const detailsMatch = detailsRegex.exec(src);
	if (detailsMatch) {
		const endIndex = findMatchingClosingTag(src, '<details', '</details>');
		if (endIndex === -1) return;

		const fullMatch = src.slice(0, endIndex);
		const detailsTag = detailsMatch[0];
		const attributes = parseAttributes(detailsTag); // Parse attributes from <details>

		let content = fullMatch.slice(detailsTag.length, -10).trim(); // Remove <details> and </details>
		let summary = '';

		const summaryMatch = summaryRegex.exec(content);
		if (summaryMatch) {
			summary = summaryMatch[1].trim();
			content = content.slice(summaryMatch[0].length).trim();
		}

		return {
			type: 'details',
			raw: fullMatch,
			summary: summary,
			text: content,
			attributes: attributes // Include extracted attributes from <details>
		};
	}
}

function detailsStart(src: string) {
	return src.match(/^<details[\s>]/) ? 0 : -1;
}

function lheadingTokenizer(this: any, src: string): any {
	const cap = this.rules.block.lheading.exec(src);
	const detailsIndex = cap?.[1]?.search(/\n<details[\s>]/) ?? -1;
	if (!cap || detailsIndex === -1) return false;

	const raw = cap[1].slice(0, detailsIndex + 1);
	const text = raw.slice(0, -1);
	return {
		type: 'paragraph',
		raw,
		text,
		tokens: this.lexer.inline(text)
	};
}

function detailsRenderer(token: any) {
	const attributesString = token.attributes
		? Object.entries(token.attributes)
				.map(([key, value]) => `${key}="${value}"`)
				.join(' ')
		: '';

	return `<details ${attributesString}>
  ${token.summary ? `<summary>${token.summary}</summary>` : ''}
  ${token.text}
  </details>`;
}

// Extension wrapper function
function detailsExtension() {
	return {
		name: 'details',
		level: 'block',
		start: detailsStart,
		tokenizer: detailsTokenizer,
		renderer: detailsRenderer
	};
}

// Inline audio extension: matches [Audio: /api/v1/files/<uuid>/content]
// emitted by MCP tool results and creates an 'audio' token for rendering.
function audioExtension() {
	return {
		name: 'audio',
		level: 'inline',
		start(src: string) {
			const idx = src.indexOf('[Audio:');
			return idx >= 0 ? idx : undefined;
		},
		tokenizer(src: string) {
			const match = /^\[Audio:\s*(\/api\/v1\/files\/[a-f0-9-]+\/content)\]/.exec(src);
			if (match) {
				return {
					type: 'audio',
					raw: match[0],
					url: match[1]
				};
			}
		},
		renderer(token: any) {
			return `<audio src="${token.url}" controls></audio>`;
		}
	};
}

// Inline 3D model extension: matches [3DModel: /api/v1/files/<id>/content]
// emitted by MCP Generate3DModel tool results (GLB files via 3d:// URIs).
function model3dExtension() {
	return {
		name: 'model3d',
		level: 'inline',
		start(src: string) {
			const idx = src.indexOf('[3DModel:');
			return idx >= 0 ? idx : undefined;
		},
		tokenizer(src: string) {
			const match = /^\[3DModel:\s*(\/api\/v1\/files\/[^\]]+\/content)\]/.exec(src);
			if (match) {
				return {
					type: 'model3d',
					raw: match[0],
					url: match[1]
				};
			}
		},
		renderer(token: any) {
			return `<span data-model3d-url="${token.url}"></span>`;
		}
	};
}

// Inline video extension: matches [Video: /api/v1/files/<uuid>/content]
// emitted by MCP EmbeddedResourceBlock tool results for video/mp4 etc.
function videoExtension() {
	return {
		name: 'video',
		level: 'inline',
		start(src: string) {
			const idx = src.indexOf('[Video:');
			return idx >= 0 ? idx : undefined;
		},
		tokenizer(src: string) {
			const match = /^\[Video:\s*(\/api\/v1\/files\/[a-f0-9-]+\/content)\]/.exec(src);
			if (match) {
				return {
					type: 'video',
					raw: match[0],
					url: match[1]
				};
			}
		},
		renderer(token: any) {
			return `<video src="${token.url}" controls></video>`;
		}
	};
}

function underlineRenderer(this: any, token: any) {
	return `<u>${this.parser.parseInline(token.tokens)}</u>`;
}

function underlineExtension() {
	return {
		name: 'underline',
		level: 'inline',
		start: (src: string) => src.indexOf('<u>'),
		tokenizer(this: any, src: string) {
			const match = /^<u>([\s\S]+?)<\/u>/.exec(src);
			if (!match) return;

			return {
				type: 'underline',
				raw: match[0],
				text: match[1],
				tokens: this.lexer.inlineTokens(match[1])
			};
		},
		renderer: underlineRenderer
	};
}

export default function (options = {}) {
	return {
		tokenizer: {
			lheading: lheadingTokenizer as any
		},
		extensions: [
			detailsExtension(),
			underlineExtension(),
			audioExtension(),
			videoExtension(),
			model3dExtension()
		]
	};
}
