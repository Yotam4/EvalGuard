// Tailwind 4 ships its PostCSS plugin as a separate package and
// no longer needs autoprefixer (handled internally).
const config = {
  plugins: ["@tailwindcss/postcss"],
};
export default config;
