# Yoho Animated Assistant — MVP Specification & Design

**Status:** Draft (prototype)

## 0. Decisions log

| # | Decision | Options considered | Choice | Decided by | Date |
| - | -------- | ------------------ | ------ | ---------- | ---- |
| 1 | Avatar animation technology | CSS/SVG only / vendored Rive / Rive from CDN | Vendored Rive, with a static-image fallback | Human | 2026-09-21 |
| 2 | How the chat page gets its assets | Inline everything in `index.html` / CDN / new static asset route | New route `GET /agents/{slug}/assets/{filename}` | Human | 2026-09-21 |
| 3 | Delivery of the sentiment path | One change / UI first, then a stacked change for Jev | UI first; Jev lands as a stacked follow-up | Human | 2026-09-21 |
| 4 | Where the sentiment plumbing lives | Only in the Jev change / generic plumbing in the UI change | Generic debounce, threshold, and race guard ship with the UI; only the classifier is added later | Agent | 2026-09-21 |
| 5 | How Yoho is animated inside `yoho.riv` | Drawn vector rig / image poses that show and hide | Seven image poses, plus small movements for idle and working | Human | 2026-09-21 |
| 6 | Names inside `yoho.riv` | Keep the `Assistant` name from the design / use the names in the authored file | Artboard `Yoho` and state machine `YohoState`; the renderer asks for both by name | Human | 2026-09-21 |
| 7 | Reduced-motion behavior | Show the static image / show Rive but stop the continuous movement / always play Rive | Always play Rive; the image stays only for a load failure. A still avatar made the demo look broken, and the movement is small and local | Human | 2026-09-21 |
| 8 | Dark edge around the avatar | Change the artboard background in the Rive Editor and export again / crop the artboard margin in the browser | Crop in the browser: `yoho-renderer.js` zooms its canvas and `.assistant-avatar` clips it. No new export is needed, and the renderer keeps all Yoho-specific numbers | Agent | 2026-09-21 |
| 9 | Where the Jev classifier runs | In the browser / in the Functions app | In the Functions app, behind `POST agents/{slug}/sentiment`. The API key never reaches the browser | Human | 2026-09-21 |
| 10 | How Jev is enabled | New front-matter key / environment variable only | `TYPESAFE_API_KEY` only. The endpoint is registered only when the key is set, and the page turns its probe off after one 404. No schema change | Human | 2026-09-21 |
| 11 | The Jev question shape | Free text / one `Choice` question | One `Choice` over `neutral`, `positive`, `concerned`, `annoyed`, with the reported confidence. The model returns a value the UI already knows | Human | 2026-09-21 |
| 12 | The `typesafe-sdk` dependency | Required / optional extra | Optional `jev` extra, imported inside the handler. A runtime without the key or the package keeps the plain avatar | Agent | 2026-09-21 |

## 1. Overview

Yoho is the default animated assistant embedded in the built-in Azure Functions Agents Runtime chat UI.

The purpose of Yoho is to make the chat experience feel more alive without adding significant complexity to the agent runtime itself.

Yoho should:

* appear alive even when idle;
* visibly react when the user types;
* change facial expression based on the sentiment or tone of the user's draft message;
* visibly enter a "working" state while the agent processes a request;
* briefly react to successful completion or failure;
* remain a UI concern as much as possible and avoid coupling animation logic to the core agent execution engine.

The implementation should prioritize simplicity over sophistication.

Because Hosted Skills is a platform, the design must also leave a clean extension point for future custom avatars.

The MVP only ships with the default Yoho avatar.

Custom avatar configuration is explicitly out of scope for the MVP.

However, the chat lifecycle, sentiment logic, and avatar state model must not depend on Yoho-specific assets or animation names.

---

# 2. Existing Runtime Integration Points

The Azure Functions Agents Runtime exposes a built-in chat UI under:

```text
/agents/{slug}/
```

The streaming chat endpoint is:

```text
POST /agents/{slug}/chatstream
```

The stream exposes SSE events including:

```text
session
delta
intermediate
tool_start
tool_end
done
error
```

The animated assistant should consume the UI's existing request lifecycle rather than introducing a separate backend execution-state service.

Repository:

https://github.com/Azure/azure-functions-agents-runtime

---

# 3. Goals

The MVP should provide a small animated character positioned next to or immediately above the chat composer.

The character must communicate four things clearly:

1. The application is alive.
2. The assistant noticed what the user is typing.
3. The assistant is currently working.
4. The assistant finished or encountered a problem.

The character should feel friendly and slightly playful, while remaining appropriate for a professional developer tool.

The implementation should also establish a minimal avatar abstraction so that future Hosted Skills can provide their own avatar without changing the chat or agent execution architecture.

---

# 4. Non-Goals

The MVP must not:

* introduce a new persistent backend service;
* modify the core agent orchestration model;
* depend on observing chain-of-thought or hidden reasoning;
* attempt to represent every tool call visually;
* generate arbitrary expressions with an LLM;
* call the sentiment model on every keypress;
* delay message submission while sentiment classification completes;
* make sentiment classification necessary for the chat UI to function;
* provide an avatar marketplace;
* provide runtime avatar uploads;
* provide custom avatar configuration UI;
* support multiple animation technologies through user configuration;
* expose avatar customization as a public Hosted Skill feature yet.

The MVP ships only with Yoho.

The abstraction for future avatars should remain intentionally small.

---

# 5. Design Principle

The central architectural rule is:

```text
Agent Runtime events
        ↓
generic assistant state
        ↓
avatar renderer
        ↓
Yoho
```

Sentiment follows a separate path:

```text
draft message
        ↓
debounced classifier
        ↓
bounded expression enum
        ↓
generic assistant state
```

Yoho must not become another agent.

The avatar should communicate the application's existing state rather than introducing a second reasoning system.

---

# 6. Separation of State and Rendering

Avatar behavior must be separated into two layers.

## Assistant state

The application decides:

```text
idle
reacting
working
success
error
```

The application does not decide which animation frames should play.

## Avatar rendering

The renderer decides how each state is visually represented.

For example:

```text
working
```

might mean:

```text
Yoho -> typing quickly on a tiny keyboard
```

while a future custom avatar might interpret the same state as:

```text
Custom avatar -> reading documents
```

The application must not care.

---

# 7. Naming

Use generic component and interface names.

Preferred:

```text
AssistantAvatar
AvatarRenderer
AssistantVisualState
AssistantExpression
```

Avoid coupling generic infrastructure to the default character.

Avoid names such as:

```text
YohoStateManager
YohoTypingAnimation
playYohoHappyJump()
```

Yoho should be the default implementation, not the abstraction itself.

Conceptually:

```text
AssistantAvatar
    ↓
DefaultYohoRenderer
```

---

# 8. Assistant Visual State

Define a small generic state model.

```ts
type AssistantExpression =
    | "neutral"
    | "positive"
    | "concerned"
    | "annoyed";

type AssistantVisualState =
    | { mode: "idle" }
    | { mode: "reacting"; expression: AssistantExpression }
    | { mode: "working" }
    | { mode: "success" }
    | { mode: "error" };
```

This API must not contain Yoho-specific animation concepts.

For example, do not expose:

```text
typing-fast
wave-left
happy-jump
look-at-keyboard
```

Those belong inside the Yoho animation implementation.

---

# 9. State Priority

Some states override others.

Priority order:

```text
error
success
working
reacting
idle
```

For example, sentiment classification must never replace the `working` animation while an agent request is active.

Conceptually:

```ts
function resolveAssistantVisualState(): AssistantVisualState {
    if (requestFailed) {
        return { mode: "error" };
    }

    if (showSuccess) {
        return { mode: "success" };
    }

    if (isWorking) {
        return { mode: "working" };
    }

    if (currentExpression) {
        return {
            mode: "reacting",
            expression: currentExpression
        };
    }

    return { mode: "idle" };
}
```

---

# 10. Default Avatar: Yoho

Yoho is the default avatar included in the product.

Recommended characteristics:

* small rounded body;
* oversized expressive eyes;
* tiny arms and hands;
* simple mouth and eyebrows;
* visually readable at approximately 48–96 px;
* minimal small details;
* distinct silhouette;
* slightly futuristic digital coworker appearance;
* suitable for professional developer tooling;
* no resemblance to an existing commercial character.

Yoho should be simple enough that multiple expressions and actions can share the same base character.

---

# 11. Avatar Renderer Abstraction

Introduce a minimal rendering boundary.

Conceptually:

```ts
interface AvatarRenderer {
    render(
        state: AssistantVisualState,
        options?: AvatarRenderOptions
    ): React.ReactNode;
}
```

Or, if the implementation fits the existing UI better:

```ts
interface AvatarController {
    setState(state: AssistantVisualState): void;
    dispose(): void;
}
```

The exact interface may follow existing frontend conventions.

The important constraint is:

```text
chat lifecycle must not directly invoke Yoho animation names
```

Instead:

```ts
setAssistantState({ mode: "working" });
```

The renderer determines how that state appears.

---

# 12. Initial Renderer

The MVP renderer is:

```text
DefaultYohoRenderer
```

Architecture:

```text
AssistantAvatar
        ↓
AvatarRenderer
        ↓
DefaultYohoRenderer
        ↓
yoho.riv
```

Only this renderer needs to be implemented in the MVP.

Do not build a plugin registry or generalized avatar loading system yet.

---

# 13. Future Avatar Extensibility

The design should allow this future architecture without changing chat-state logic:

```text
AssistantAvatar
    ↓
AvatarRenderer
    ├── DefaultYohoRenderer
    ├── CustomRiveRenderer
    ├── StaticImageRenderer
    └── FutureRenderer
```

This is a future extension point, not an MVP requirement.

Possible future Hosted Skill metadata might look like:

```json
{
  "name": "My Agent",
  "avatar": {
    "type": "rive",
    "src": "./my-agent.riv"
  }
}
```

Or:

```json
{
  "avatar": {
    "type": "image",
    "src": "./avatar.png"
  }
}
```

These configuration formats are illustrative only.

Do not commit to them as a public schema in the MVP.

---

# 14. Static Avatar Compatibility

A future avatar system should not require every avatar to implement complex animation.

A static image avatar should still be able to participate in the generic visual state model.

For example:

```text
idle
    -> normal image

working
    -> CSS pulse

success
    -> brief CSS scale

error
    -> brief CSS shake
```

Therefore, generic states should describe meaning rather than animation mechanics.

---

# 15. Idle Behavior

When there is no active request and no stronger reaction state, the assistant should appear subtly alive.

The Yoho idle animation should loop continuously.

Recommended micro-animations:

* blinking;
* breathing or very small vertical movement;
* slight head movement;
* occasionally looking left or right;
* occasional small hand movement.

Idle behavior should remain subtle enough not to distract from reading the conversation.

Recommended timing:

```text
base idle loop:
3–6 seconds

blink:
approximately every 2–7 seconds

larger idle gesture:
approximately every 8–20 seconds
```

Where possible, this timing should live inside the animation state machine rather than application JavaScript.

---

# 16. Working Behavior

When the user submits a message, the assistant immediately enters the generic:

```text
working
```

state.

Do not wait for the server to acknowledge the request.

```text
message submitted
    ↓
working
```

The state remains active until the current chat stream completes or fails.

Relevant SSE behavior:

```text
done
    ↓
success

error
    ↓
error
```

`tool_start` and `tool_end` should not alter the primary avatar state in the MVP.

For Yoho, the `working` state may visually show:

* typing rapidly on a tiny keyboard;
* looking between screens;
* handling documents;
* concentrating intensely.

Only one working animation is required.

---

# 17. Success Behavior

When the SSE stream produces:

```text
done
```

the assistant enters:

```text
success
```

Suggested duration:

```text
800–1500 ms
```

Yoho may:

* perform a small fist pump;
* smile and nod;
* show a tiny sparkle;
* give a satisfied expression.

Afterward:

```text
success
    ↓
idle
```

The application should only know the semantic state `success`.

The Yoho renderer owns the visual interpretation.

---

# 18. Error Behavior

When the stream fails or produces:

```text
error
```

the assistant enters:

```text
error
```

Suggested duration:

```text
1500–2500 ms
```

Yoho may:

* look surprised;
* appear concerned;
* scratch its head;
* perform a small "oops" gesture.

The reaction should not be melodramatic.

Afterward:

```text
error
    ↓
idle
```

The existing UI error message remains authoritative.

The avatar provides supplementary visual feedback only.

---

# 19. Typing Reaction

When the user types while no request is running, the assistant may react to the draft message.

Sentiment analysis must not run when:

```text
isWorking === true
```

The input should be debounced.

Recommended initial debounce:

```text
700 ms
```

Do not classify extremely short input.

Recommended minimum:

```text
10–15 characters
```

Pipeline:

```text
user types
    ↓
debounce
    ↓
minimum length check
    ↓
sentiment classification
    ↓
confidence threshold
    ↓
AssistantExpression
    ↓
reacting state
```

Classification must never block typing or message submission.

---

# 20. Sentiment Classification

Use Jev or another low-latency bounded classification mechanism.

The classifier should return a fixed enum rather than arbitrary natural-language output.

Recommended schema:

```json
{
  "sentiment": "positive | neutral | concerned | annoyed",
  "confidence": 0.0
}
```

The sentiment values map directly to generic `AssistantExpression` values.

## 20.1 As built

`src/azure_functions_agents/sentiment.py` holds the classifier. It asks the
TypeSafe System One API (the Jev model) one `Choice` question over the four
values above and returns the selected value with the reported confidence.

`POST agents/{slug}/sentiment` exposes it, with the body `{"draft": "..."}`.
The endpoint is registered only when `TYPESAFE_API_KEY` is set, so a
deployment without a key keeps the plain avatar and the chat page turns its
probe off after the first 404.

The module never raises. A missing key, a missing `typesafe-sdk` package, a
timeout, or an answer outside the four values all return
`{"sentiment": "neutral", "confidence": 0.0}`.

The classifier must not produce animation names.

---

# 21. Expression Semantics

## positive

Example input:

```text
Great, let's implement this.
That worked!
Nice. Can we add one more thing?
```

Meaning:

```text
friendly / pleased / engaged
```

Yoho implementation:

* smile;
* brighter eyes;
* mildly excited posture.

---

## neutral

Example:

```text
Please update the configuration.
Explain this function.
Add tests for this method.
```

Meaning:

```text
attentive / calm / listening
```

Yoho implementation:

* normal attentive expression.

---

## concerned

Example:

```text
Something seems wrong here.
I'm worried this may break production.
Why did this fail?
```

Meaning:

```text
sympathetic / focused / mildly worried
```

Yoho implementation:

* raised or angled eyebrows;
* focused eyes.

---

## annoyed

Example:

```text
This keeps failing and it's really annoying.
Why is this so complicated?
I have tried this three times already.
```

Meaning:

```text
recognition that the situation is frustrating
```

Yoho should appear:

* slightly weary;
* determined;
* empathetic.

It must not appear amused at or mocking the user.

---

# 22. Sentiment Confidence

Do not change expression for low-confidence classifications.

Suggested threshold:

```text
confidence >= 0.65
```

Otherwise:

```text
neutral
```

The threshold may be tuned later.

---

# 23. Race Conditions

Typing classification responses may arrive out of order.

Each request should use:

* an incrementing request identifier; or
* the exact draft text used for classification.

Example:

```ts
const requestId = ++sentimentRequestId;

const result = await classify(text);

if (requestId !== sentimentRequestId) {
    return;
}
```

If the user submits while classification is active:

```text
working state wins immediately
```

A later classification response must not replace the working state.

---

# 24. Suggested Frontend Structure

Suggested logical structure:

```text
Chat UI
├── ChatMessages
├── Composer
└── AssistantAvatar
    ├── useAssistantVisualState
    ├── useDraftSentiment
    └── renderers
        └── DefaultYohoRenderer
```

Do not significantly restructure the existing chat UI solely for this feature.

---

# 25. State Hook

Suggested conceptual API:

```ts
interface AssistantAvatarStateController {
    onDraftChanged(text: string): void;
    onRequestStarted(): void;
    onRequestCompleted(): void;
    onRequestFailed(): void;
}
```

Internally it exposes:

```ts
AssistantVisualState
```

The hook or controller must not expose Yoho-specific concepts.

---

# 26. Animation Technology

Preferred implementation for the default Yoho avatar:

```text
Rive
```

Reasons:

* vector animation;
* small reusable assets;
* state machines;
* programmatic state control;
* continuous idle behavior;
* good scaling at small UI sizes.

One `.riv` file should contain Yoho and all MVP animation states.

Suggested Rive state-machine inputs:

```text
working: boolean
success: trigger
error: trigger
expression: number
```

Possible expression mapping:

```text
0 = neutral
1 = positive
2 = concerned
3 = annoyed
```

These Rive-specific details must remain inside `DefaultYohoRenderer`.

The rest of the application should not know how the `.riv` file is structured.

---

# 27. Renderer Responsibility

The renderer owns:

* animation asset loading;
* mapping generic states to animation states;
* transitions between animations;
* internal idle micro-animation;
* cleanup;
* rendering fallback.

The application owns:

* request lifecycle;
* generic visual state;
* sentiment expression;
* state priority.

This boundary should remain strict.

---

# 28. Fallback

If the Yoho Rive asset cannot load:

* render a static Yoho image;
* do not show an error to the user;
* do not affect chat functionality.

If sentiment classification fails:

```text
expression = neutral
```

Do not retry aggressively.

If the avatar renderer fails entirely, the rest of chat must continue functioning normally.

---

# 29. Performance Requirements

The avatar must not materially affect chat responsiveness.

Targets:

* visual state transition on submit: immediate;
* sentiment classification: asynchronous;
* no synchronous model calls from input handlers;
* no frame-by-frame React state updates;
* browser-native smooth animation where practical;
* avatar animation loaded once rather than reloaded on state changes.

The avatar rendering layer should remain lightweight.

---

# 30. Accessibility

Respect:

```css
@media (prefers-reduced-motion: reduce)
```

With reduced motion enabled:

* disable continuous body movement;
* use static or low-motion expressions;
* allow simple state changes;
* never require animation to understand request status.

Provide an accessible label such as:

```text
Assistant: idle
Assistant: working
Assistant: completed
Assistant: error
```

Do not require the accessible description to use the name Yoho because future avatars may differ.

---

# 31. Privacy

Draft text used for sentiment classification is user content.

The implementation must make the data path explicit.

If Jev requires remote inference, sentiment classification should be configurable and only enabled when the product is comfortable sending draft content to that inference path.

Do not retain draft messages solely for avatar sentiment.

Do not log raw draft text for avatar telemetry.

Prefer local or already-approved inference paths where practical.

---

# 32. Configuration

Keep MVP configuration minimal.

Conceptually:

```ts
assistantAvatar: {
    enabled: true,
    sentimentEnabled: true
}
```

Do not expose avatar provider selection in the MVP.

Do not introduce configuration such as:

```text
avatar.type
avatar.src
avatar.renderer
```

until custom avatars are actually supported.

---

# 33. Future Hosted Skill Configuration

The implementation should leave room for Hosted Skills to specify an avatar later.

Potential conceptual configuration:

```json
{
  "name": "My Hosted Skill",
  "avatar": {
    "type": "rive",
    "src": "./assets/avatar.riv"
  }
}
```

Or:

```json
{
  "avatar": {
    "type": "image",
    "src": "./assets/avatar.png"
  }
}
```

This section documents architectural intent only.

Do not implement or expose this schema during MVP work.

A future design should separately consider:

* asset security;
* allowed origins;
* file-size limits;
* renderer compatibility;
* untrusted animation assets;
* accessibility requirements;
* fallback behavior.

---

# 34. Telemetry

Only minimal telemetry is justified initially.

Potential counters:

```text
assistant_avatar.loaded
assistant_avatar.load_failed
assistant_avatar.sentiment_request
assistant_avatar.sentiment_error
```

Avoid names tied exclusively to Yoho if telemetry is expected to survive future customization.

Do not log message content.

Do not add expression analytics unless a concrete product requirement appears.

---

# 35. MVP Acceptance Criteria

The implementation is complete when:

1. The default Yoho avatar appears in the built-in chat UI.
2. Yoho performs a subtle looping idle animation.
3. Submitting a message immediately switches the generic assistant state to `working`.
4. Yoho visually represents the working state.
5. Yoho remains working while the SSE request is active.
6. A `done` event produces a brief success animation.
7. An `error` event produces a brief error animation.
8. While idle, typing a sufficiently long message triggers debounced sentiment classification.
9. Positive, neutral, concerned, and annoyed classifications produce distinct expressions.
10. Sentiment never overrides `working`.
11. Stale sentiment responses cannot overwrite newer state.
12. Failure of sentiment classification does not affect chat.
13. Failure of the Rive asset does not affect chat.
14. Reduced-motion preferences are respected.
15. Existing chat and session behavior remain unchanged.
16. Chat lifecycle code does not reference Yoho-specific animation names.
17. Generic avatar state and avatar rendering are separate modules.
18. The default Yoho renderer can theoretically be replaced without changing the request lifecycle or sentiment logic.

---

# 36. Tests

## State unit tests

Test:

```text
idle -> working
working -> success -> idle
working -> error -> idle
idle -> reacting
reacting -> working
```

Test priority:

```text
working overrides sentiment
error overrides weaker states
success eventually returns to idle
```

Test stale sentiment requests.

Test sentiment failure fallback.

---

## Renderer boundary tests

Verify that generic state is passed into the renderer.

For example:

```text
AssistantVisualState = working
```

should cause the renderer to receive:

```text
working
```

without application code referencing a concrete Rive animation.

Mock the renderer where useful.

The generic state logic should be testable without loading Rive.

---

## UI / integration tests

Verify:

```text
submit message
    -> working state

SSE done
    -> success
    -> idle

SSE error
    -> error
    -> idle
```

Verify typing debounce.

Verify no sentiment request during working state.

Verify normal chat works when the assistant avatar is disabled.

Verify the UI still works if the avatar renderer fails.

---

# 37. Suggested Implementation Order

Implement in this order:

```text
1. Define generic AssistantVisualState
2. Add AssistantAvatar component
3. Add AvatarRenderer boundary
4. Add DefaultYohoRenderer
5. Render static Yoho
6. Integrate Rive
7. Implement idle animation
8. Connect working state to existing request lifecycle
9. Implement success/error states
10. Add sentiment interface with mocked classifier
11. Add Jev integration
12. Add reduced-motion behavior
13. Add tests
```

Do not begin with sentiment.

Do not begin with custom avatar support.

First prove:

```text
generic state
    ↓
renderer abstraction
    ↓
Yoho
```

and:

```text
idle -> working -> success/error -> idle
```

---

# 38. Future Extensions

After the MVP proves useful, possible extensions include:

```text
custom Hosted Skill avatars
tool-specific avatar activities
multiple working animations
sub-agent visualization
user-selectable themes
```

Possible specialized states may include:

```text
searching
coding
waiting
delegating
```

For example:

```text
tool_start(web)
    -> searching

tool_start(code)
    -> coding

sub-agent invocation
    -> delegating
```

Yoho could visualize them as:

```text
searching
    -> magnifying glass

coding
    -> keyboard / terminal

delegating
    -> calling another tiny assistant
```

However, these extensions should remain separate from the MVP.

Do not expand the state model until there is a concrete UX benefit.

---

# 39. Architectural Constraint for Future Avatars

Future avatar extensibility must not require changing:

* SSE handling;
* message submission logic;
* sentiment classification;
* state priority;
* chat session management.

Only the rendering layer should need replacement.

Desired long-term architecture:

```text
                       ┌──────────────────────┐
draft text ───────────►│ sentiment classifier │
                       └──────────┬───────────┘
                                  │
                                  ▼
Chat lifecycle ─────────► AssistantVisualState
                                  │
                                  ▼
                          AssistantAvatar
                                  │
                                  ▼
                           AvatarRenderer
                         ┌────────┴─────────┐
                         │                  │
                         ▼                  ▼
                  Default Yoho       Future Avatar
```

This is the primary extensibility requirement.

---

# 40. Implementation Guidance

Prefer the smallest implementation that preserves the architectural boundary.

Do not build infrastructure merely because future custom avatars are possible.

For the MVP:

```text
one generic state model
one renderer interface
one Yoho renderer
one Rive asset
```

is sufficient.

The extension point should exist structurally, but there should be no generalized plugin system, registration framework, dynamic asset discovery, or custom-avatar configuration yet.

The intended principle is:

**implement one avatar, design for more than one.**
