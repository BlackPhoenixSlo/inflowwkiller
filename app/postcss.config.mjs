// Tailwind 4 ships its PostCSS plugin as @tailwindcss/postcss.
// No autoprefixer needed — Next's compiler already handles vendor prefixes.
const config = {
  plugins: ["@tailwindcss/postcss"],
};

export default config;
