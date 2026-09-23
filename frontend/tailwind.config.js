/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Bricolage Grotesque"', "sans-serif"],
        mono:    ['"IBM Plex Mono"', "ui-monospace", "monospace"],
      },
      animation: {
        "fade-in-up": "fade-in-up 0.18s ease-out both",
      },
      keyframes: {
        "fade-in-up": {
          from: { opacity: "0", transform: "translateY(6px)" },
          to:   { opacity: "1", transform: "translateY(0)"   },
        },
      },
    },
  },
  plugins: [],
}
