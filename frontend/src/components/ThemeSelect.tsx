import { useEffect, useState } from "react";

export const THEMES = [
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
  { id: "cyberpunk", label: "Cyberpunk" },
  { id: "synthwave", label: "Synthwave" },
  { id: "terminal", label: "Terminal" },
  { id: "amber", label: "Amber CRT" },
];

export function applyTheme(theme: string) {
  document.documentElement.dataset.theme = theme;
}

export default function ThemeSelect() {
  const [theme, setTheme] = useState(
    () => localStorage.getItem("synapse-theme") || "light"
  );

  useEffect(() => {
    applyTheme(theme);
    localStorage.setItem("synapse-theme", theme);
  }, [theme]);

  return (
    <select
      className="themeselect"
      title="UI theme"
      value={theme}
      onChange={(e) => setTheme(e.target.value)}
    >
      {THEMES.map((t) => (
        <option key={t.id} value={t.id}>{t.label}</option>
      ))}
    </select>
  );
}
