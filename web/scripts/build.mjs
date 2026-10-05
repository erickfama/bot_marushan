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
    <title>Bot Marushan</title>
    <link rel="stylesheet" href="/assets/app.css" />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/assets/app.js"></script>
  </body>
</html>
`,
  "utf8",
);
