// Palette taken from the BlockBucks Figma suite (green primary, deep green accents, mint panels).
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand: { DEFAULT: "#5BA58C", strong: "#4A9179", deep: "#1E3B32", ink: "#14251F" },
        mint: { DEFAULT: "#E9F4EF", line: "#A8D5C3", text: "#3E8A70" },
        canvas: "#F2F4F3",
        muted: "#6B7A74",
        danger: { DEFAULT: "#D64545", pale: "#FCEEEE", line: "#F2B8B8" },
        warn: { DEFAULT: "#B7791F", pale: "#FFF6E6" },
      },
      fontFamily: {
        sans: ["Pretendard", "Inter", "Apple SD Gothic Neo", "Malgun Gothic", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      boxShadow: { card: "0 1px 2px rgba(20,37,31,0.04), 0 4px 16px rgba(20,37,31,0.05)" },
      keyframes: {
        ripple: { "0%": { transform: "scale(0.8)", opacity: "0.6" }, "100%": { transform: "scale(2.2)", opacity: "0" } },
        pop: { "0%": { transform: "scale(0.92)", opacity: "0" }, "100%": { transform: "scale(1)", opacity: "1" } },
      },
      animation: { ripple: "ripple 1.8s ease-out infinite", pop: "pop 0.25s ease-out" },
    },
  },
  plugins: [],
};
