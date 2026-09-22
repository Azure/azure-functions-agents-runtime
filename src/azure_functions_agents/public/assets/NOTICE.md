# Third-party assets

## rive.js

Vendored copy of [`@rive-app/canvas-single`](https://www.npmjs.com/package/@rive-app/canvas-single)
version 2.42.2, published by Rive under the MIT license.

The file is committed instead of loaded from a CDN so the built-in chat UI keeps
working offline and behind restrictive network policies. The single-file build
embeds the WebAssembly module, so no second request is needed.

To update it, download the same file again:

```bash
curl -sL -o rive.js https://unpkg.com/@rive-app/canvas-single@<version>/rive.js
```

## yoho.png

Static fallback image for the default assistant avatar. It is shown when the
Rive runtime or `yoho.riv` cannot load.

## yoho.riv

Yoho animation made in the Rive editor and supplied as `yoho-logo.riv`.
The application stores it as `yoho.riv`.

Artboard: `Yoho`. State machine: `YohoState`.

Inputs: `working` (boolean), `success` and `error` (triggers), and `expression`
(number). Expression values are 0 for neutral, 1 for positive, 2 for concerned,
and 3 for annoyed. Success and error each show a pose for one second.

The seven poses use embedded images. Idle and working also use small movements.
If the file cannot load, `yoho-renderer.js` uses `yoho.png` with CSS animation.
