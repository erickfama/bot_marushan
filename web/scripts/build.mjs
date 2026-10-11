import { mkdir, rm, writeFile } from "node:fs/promises";
import { build } from "esbuild";

await rm("dist", { recursive: true, force: true });
await mkdir("dist/assets", { recursive: true });

await build({
  entryPoints: { app: "src/main.tsx" },
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ["es2022"],
  format: "esm",
  outdir: "dist/assets",
  entryNames: "[name]",
  assetNames: "[name]",
  logLevel: "info",
});

await writeFile(
  "dist/index.html",
  `<!doctype html>
<html lang="es">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#101018" />
    <title>Bot Nissin</title>
    <link rel="manifest" href="/manifest.webmanifest" />
    <link rel="stylesheet" href="/assets/app.css" />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/assets/app.js"></script>
    <script>if('serviceWorker' in navigator){addEventListener('load',()=>navigator.serviceWorker.register('/service-worker.js'))}</script>
  </body>
</html>
`,
  "utf8",
);

await writeFile(
  "dist/manifest.webmanifest",
  JSON.stringify({
    name: "Bot Nissin",
    short_name: "Nissin",
    description: "Reproductor musical personal para Discord",
    start_url: "/",
    display: "standalone",
    background_color: "#0d0d12",
    theme_color: "#ff6b42",
    lang: "es-MX",
  }),
  "utf8",
);

await writeFile(
  "dist/service-worker.js",
  `const CACHE='nissin-shell-v1';
const SHELL=['/','/assets/app.js','/assets/app.css','/manifest.webmanifest'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL))));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key))))));
self.addEventListener('fetch',event=>{if(event.request.method!=='GET'||new URL(event.request.url).pathname.startsWith('/api/'))return;event.respondWith(fetch(event.request).then(response=>{const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));return response}).catch(()=>caches.match(event.request).then(hit=>hit||caches.match('/'))))});
`,
  "utf8",
);
