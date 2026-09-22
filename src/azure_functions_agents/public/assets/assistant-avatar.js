// Generic assistant avatar state and mounting.
//
// This module owns the *meaning* of what the assistant shows. It must stay
// free of artwork, animation names, and character names. A renderer decides
// how each state looks. See docs/frds/0009-animated-assistant-avatar.md.

/**
 * @typedef {"neutral" | "positive" | "concerned" | "annoyed"} AssistantExpression
 * @typedef {{ mode: "idle" }
 *   | { mode: "reacting", expression: AssistantExpression }
 *   | { mode: "working" }
 *   | { mode: "success" }
 *   | { mode: "error" }} AssistantVisualState
 */

export const ASSISTANT_EXPRESSIONS = ["neutral", "positive", "concerned", "annoyed"];

const SUCCESS_MS = 1200;
const ERROR_MS = 2000;

const ACCESSIBLE_LABELS = {
	idle: "Assistant: idle",
	reacting: "Assistant: idle",
	working: "Assistant: working",
	success: "Assistant: completed",
	error: "Assistant: error"
};

/**
 * Hold the inputs that decide the visual state, and apply the priority order
 * error > success > working > reacting > idle.
 */
export function createAssistantVisualState({ onChange }) {
	let working = false;
	let success = false;
	let failed = false;
	let expression = null;
	let timer = null;
	let last = null;

	function resolve() {
		if (failed) return { mode: "error" };
		if (success) return { mode: "success" };
		if (working) return { mode: "working" };
		if (expression && expression !== "neutral") {
			return { mode: "reacting", expression };
		}
		return { mode: "idle" };
	}

	function emit() {
		const next = resolve();
		const key = `${next.mode}:${next.expression || ""}`;
		if (key === last) return;
		last = key;
		onChange(next);
	}

	function clearTimer() {
		if (timer !== null) {
			clearTimeout(timer);
			timer = null;
		}
	}

	return {
		/** The request lifecycle always wins over a typing reaction. */
		requestStarted() {
			clearTimer();
			working = true;
			success = false;
			failed = false;
			expression = null;
			emit();
		},
		requestCompleted() {
			clearTimer();
			working = false;
			success = true;
			failed = false;
			emit();
			timer = setTimeout(() => {
				success = false;
				timer = null;
				emit();
			}, SUCCESS_MS);
		},
		requestFailed() {
			clearTimer();
			working = false;
			success = false;
			failed = true;
			emit();
			timer = setTimeout(() => {
				failed = false;
				timer = null;
				emit();
			}, ERROR_MS);
		},
		/** Ignored while a request runs, so sentiment can never hide `working`. */
		setExpression(next) {
			if (working || success || failed) return;
			if (next !== null && !ASSISTANT_EXPRESSIONS.includes(next)) return;
			expression = next;
			emit();
		},
		isWorking() {
			return working;
		},
		current() {
			return resolve();
		},
		dispose() {
			clearTimer();
		}
	};
}

/**
 * Build the avatar DOM, attach a renderer, and return the controller the chat
 * lifecycle drives. The controller exposes no renderer-specific concept.
 */
export async function mountAssistantAvatar({ mount, assetsBase, createRenderer, classifyDraft }) {
	const reducedMotion =
		typeof window.matchMedia === "function" &&
		window.matchMedia("(prefers-reduced-motion: reduce)").matches;

	mount.setAttribute("role", "img");
	mount.setAttribute("aria-label", ACCESSIBLE_LABELS.idle);

	let renderer = null;
	const visual = createAssistantVisualState({
		onChange: (next) => {
			mount.setAttribute("aria-label", ACCESSIBLE_LABELS[next.mode] || ACCESSIBLE_LABELS.idle);
			// A broken renderer must never break chat.
			try {
				renderer?.setState(next);
			} catch (_error) {
				/* ignored */
			}
		}
	});

	try {
		renderer = await createRenderer({ mount, assetsBase, reducedMotion });
		renderer.setState(visual.current());
	} catch (_error) {
		renderer = null;
	}

	const draft = createDraftReaction({
		classify: classifyDraft,
		isBlocked: () => visual.isWorking(),
		onExpression: (expression) => visual.setExpression(expression)
	});

	return {
		onDraftChanged(text) {
			draft.onDraftChanged(text);
		},
		onRequestStarted() {
			draft.cancel();
			visual.requestStarted();
		},
		onRequestCompleted() {
			visual.requestCompleted();
		},
		onRequestFailed() {
			visual.requestFailed();
		},
		dispose() {
			draft.cancel();
			visual.dispose();
			try {
				renderer?.dispose();
			} catch (_error) {
				/* ignored */
			}
		}
	};
}

const DEBOUNCE_MS = 700;
const MIN_DRAFT_LENGTH = 12;
const CONFIDENCE_THRESHOLD = 0.65;

/**
 * Debounce the draft, drop short text, guard against out-of-order replies, and
 * map a bounded classification to an expression. The classifier itself is
 * injected; without one this stays inert.
 */
export function createDraftReaction({ classify, isBlocked, onExpression }) {
	let timer = null;
	let requestId = 0;

	function cancel() {
		if (timer !== null) {
			clearTimeout(timer);
			timer = null;
		}
		requestId += 1;
	}

	return {
		cancel,
		onDraftChanged(text) {
			if (typeof classify !== "function") return;

			cancel();
			const draft = (text || "").trim();
			if (draft.length < MIN_DRAFT_LENGTH || isBlocked()) {
				onExpression(null);
				return;
			}

			timer = setTimeout(async () => {
				timer = null;
				const id = ++requestId;
				let result = null;
				try {
					result = await classify(draft);
				} catch (_error) {
					result = null;
				}

				// A stale reply, or one that arrived after submission, is dropped.
				if (id !== requestId || isBlocked()) return;

				const confidence = Number(result?.confidence);
				if (!result || !(confidence >= CONFIDENCE_THRESHOLD)) {
					onExpression("neutral");
					return;
				}

				onExpression(
					ASSISTANT_EXPRESSIONS.includes(result.sentiment) ? result.sentiment : "neutral"
				);
			}, DEBOUNCE_MS);
		}
	};
}
