// Default avatar renderer: Yoho.
//
// Everything specific to Yoho lives here — the Rive file name, its state
// machine inputs, and the CSS classes used by the static fallback. The rest of
// the app only passes a generic AssistantVisualState.

const RIVE_RUNTIME_FILE = "rive.js";
const RIVE_ASSET_FILE = "yoho.riv";
const IMAGE_ASSET_FILE = "yoho.png";
const ARTBOARD = "Yoho";
const STATE_MACHINE = "YohoState";

// Rive state machine inputs owned by yoho.riv.
const INPUT_WORKING = "working";
const INPUT_SUCCESS = "success";
const INPUT_ERROR = "error";
const INPUT_EXPRESSION = "expression";

const EXPRESSION_NUMBERS = {
	neutral: 0,
	positive: 1,
	concerned: 2,
	annoyed: 3
};

// A runtime or asset that never answers must not stall the avatar forever.
const LOAD_TIMEOUT_MS = 5000;

// The Yoho poses fill 384 of the 500-unit artboard, and idle and working move
// them a little. The canvas is zoomed so the mount clips the artboard
// background away in every pose.
const POSE_ZOOM = 500 / 350;

function withTimeout(promise, message) {
	return Promise.race([
		promise,
		new Promise((_resolve, reject) => {
			setTimeout(() => reject(new Error(message)), LOAD_TIMEOUT_MS);
		})
	]);
}

const MODE_CLASSES = ["is-idle", "is-reacting", "is-working", "is-success", "is-error"];
const EXPRESSION_CLASSES = ["expr-neutral", "expr-positive", "expr-concerned", "expr-annoyed"];

let riveRuntimePromise = null;

function loadRiveRuntime(assetsBase) {
	if (window.rive) return Promise.resolve(window.rive);
	if (riveRuntimePromise) return riveRuntimePromise;

	riveRuntimePromise = new Promise((resolve, reject) => {
		const script = document.createElement("script");
		script.src = `${assetsBase}/${RIVE_RUNTIME_FILE}`;
		script.async = true;
		script.onload = () => {
			if (window.rive) resolve(window.rive);
			else reject(new Error("Rive runtime loaded without a global."));
		};
		script.onerror = () => reject(new Error("Rive runtime failed to load."));
		document.head.appendChild(script);
	});

	return riveRuntimePromise;
}

function applyStateClasses(mount, state) {
	mount.classList.remove(...MODE_CLASSES, ...EXPRESSION_CLASSES);
	mount.classList.add(`is-${state.mode}`);
	if (state.mode === "reacting") {
		mount.classList.add(`expr-${state.expression}`);
	}
}

/**
 * Create the Yoho renderer. Falls back to a static image whenever the Rive
 * runtime or the .riv asset is unavailable — chat must keep working either way.
 *
 * @returns {Promise<{ setState: (state: object) => void, dispose: () => void }>}
 */
export async function createDefaultYohoRenderer({ mount, assetsBase, reducedMotion }) {
	const image = document.createElement("img");
	image.className = "assistant-avatar-image";
	image.src = `${assetsBase}/${IMAGE_ASSET_FILE}`;
	image.alt = "";
	image.decoding = "async";
	mount.appendChild(image);

	const fallbackRenderer = {
		setState(state) {
			applyStateClasses(mount, state);
		},
		dispose() {
			mount.classList.remove("is-reduced-motion", ...MODE_CLASSES, ...EXPRESSION_CLASSES);
			image.remove();
		}
	};

	// Reduced motion stops the CSS effects of the page. The Rive avatar keeps
	// playing, because the pose is the only thing that shows the agent state.
	if (reducedMotion) {
		mount.classList.add("is-reduced-motion");
	}

	let riveInstance = null;
	try {
		const runtime = await withTimeout(
			loadRiveRuntime(assetsBase),
			"Rive runtime timed out."
		);
		const canvas = document.createElement("canvas");
		canvas.className = "assistant-avatar-canvas";
		canvas.width = 192;
		canvas.height = 192;
		canvas.style.transform = `scale(${POSE_ZOOM})`;
		mount.appendChild(canvas);

		riveInstance = await withTimeout(
			new Promise((resolve, reject) => {
				const instance = new runtime.Rive({
					src: `${assetsBase}/${RIVE_ASSET_FILE}`,
					canvas,
					autoplay: true,
					artboard: ARTBOARD,
					stateMachines: STATE_MACHINE,
					onLoad: () => resolve(instance),
					onLoadError: () => reject(new Error("Yoho Rive asset failed to load."))
				});
			}),
			"Yoho Rive asset timed out."
		);

		image.remove();
		mount.classList.add("has-rive");

		const inputs = riveInstance.stateMachineInputs(STATE_MACHINE) || [];
		const findInput = (name) => inputs.find((input) => input.name === name) || null;
		const working = findInput(INPUT_WORKING);
		const success = findInput(INPUT_SUCCESS);
		const error = findInput(INPUT_ERROR);
		const expression = findInput(INPUT_EXPRESSION);

		return {
			setState(state) {
				applyStateClasses(mount, state);
				if (working) working.value = state.mode === "working";
				if (expression) {
					expression.value =
						state.mode === "reacting"
							? EXPRESSION_NUMBERS[state.expression] ?? EXPRESSION_NUMBERS.neutral
							: EXPRESSION_NUMBERS.neutral;
				}
				if (state.mode === "success") success?.fire();
				if (state.mode === "error") error?.fire();
			},
			dispose() {
				mount.classList.remove(
					"has-rive",
					"is-reduced-motion",
					...MODE_CLASSES,
					...EXPRESSION_CLASSES
				);
				riveInstance?.cleanup();
				canvas.remove();
			}
		};
	} catch (_error) {
		// No Rive: the static image plus CSS still expresses every state.
		riveInstance?.cleanup();
		mount.querySelector(".assistant-avatar-canvas")?.remove();
		if (!image.isConnected) mount.appendChild(image);
		return fallbackRenderer;
	}
}
