/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: {
          50: '#f0f9ff',
          100: '#e8efff',
          200: '#d4e1ff',
          300: '#b4caff',
          400: '#93b4ff',
          500: '#78a2ff',
          600: '#5d86ea',
          700: '#496bbd',
          800: '#334f91',
          900: '#293f73',
        },
      },
    },
  },
  plugins: [],
}
