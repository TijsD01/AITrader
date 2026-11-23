# Frontend

## Rebuilding Tailwind CSS

`src/web/static/tailwind.css` is prebuilt and committed (Tailwind 3.4.1, minified). `index.html` loads it before `style.css`. Config: `tailwind.config.js`. Input: `tailwind.input.css`.

Rebuild after you add or change a Tailwind class in `index.html`, `app.js` or `features.js`, or edit `tailwind.config.js`:

```bash
npx -y tailwindcss@3.4.1 -c tailwind.config.js -i tailwind.input.css -o src/web/static/tailwind.css --minify
```

Tailwind only emits classes it finds as complete strings. Never build one with `'text-' + x` or `` `bg-${x}` ``. Write the full class names, or add them to `safelist` in `tailwind.config.js`.
