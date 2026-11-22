/** Inline stroke icons (no icon font, nothing from the network). 20×20, currentColor, 1.6 px stroke. */
const PATHS: Record<string, string> = {
  dashboard: "M3 3h6v8H3zM11 3h6v5h-6zM11 10h6v7h-6zM3 13h6v4H3z",
  play: "M6 4l10 6-10 6z",
  plus: "M10 4v12M4 10h12",
  campaigns: "M3 5h14M3 10h14M3 15h9",
  graph: "M5 5m-2 0a2 2 0 104 0a2 2 0 10-4 0M15 6m-2 0a2 2 0 104 0a2 2 0 10-4 0M10 15m-2 0a2 2 0 104 0a2 2 0 10-4 0M7 6l6 0M6 7l3 6M14 8l-3 5",
  pairs: "M3 4h6v12H3zM11 4h6v12h-6z",
  queue: "M4 5h12M4 10h12M4 15h7M15 13l2 2-2 2",
  incidents: "M4 3h9l3 3v11H4zM13 3v3h3",
  search: "M9 9m-5 0a5 5 0 1010 0a5 5 0 10-10 0M13 13l4 4",
  clock: "M10 10m-7 0a7 7 0 1014 0a7 7 0 10-14 0M10 6v4l3 2",
  chart: "M4 16V8M9 16V4M14 16v-6M3 16h14",
  upload: "M10 13V4M6 8l4-4 4 4M4 16h12",
  settings: "M10 10m-2.5 0a2.5 2.5 0 105 0a2.5 2.5 0 10-5 0M10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.4 1.4M13.6 13.6L15 15M5 15l1.4-1.4M13.6 6.4L15 5",
  sun: "M10 10m-3.5 0a3.5 3.5 0 107 0a3.5 3.5 0 10-7 0M10 2v2M10 16v2M2 10h2M16 10h2M4.3 4.3l1.4 1.4M14.3 14.3l1.4 1.4M4.3 15.7l1.4-1.4M14.3 5.7l1.4-1.4",
  moon: "M15.5 12.5A6.5 6.5 0 017.5 4.5a6.5 6.5 0 108 8z",
  menu: "M3 5h14M3 10h14M3 15h14",
  close: "M5 5l10 10M15 5L5 15",
  panel: "M3 4h14v12H3zM7 4v12",
  help: "M10 10m-7 0a7 7 0 1014 0a7 7 0 10-14 0M8 8a2 2 0 113 1.7c-.7.4-1 .8-1 1.6M10 14h.01",
  eye: "M2 10s3-5.5 8-5.5S18 10 18 10s-3 5.5-8 5.5S2 10 2 10zM10 10m-2 0a2 2 0 104 0a2 2 0 10-4 0",
  layers: "M10 3l7 4-7 4-7-4zM3 11l7 4 7-4",
  flow: "M3 5h4v4H3zM13 11h4v4h-4zM7 7h3a2 2 0 012 2v2a2 2 0 002 2",
  check: "M4 3h12v14H4zM7 10l2 2 4-4",
  download: "M10 4v9M6 9l4 4 4-4M4 16h12",
  chain: "M8.5 11.5l3-3M7 9l-2 2a2.5 2.5 0 003.5 3.5l2-2M13 11l2-2a2.5 2.5 0 00-3.5-3.5l-2 2",
  alert: "M10 3l8 14H2zM10 8v4M10 14.5h.01",
  bell: "M6 8a4 4 0 118 0v3l1.5 3h-11L6 11zM8.5 16.5a1.6 1.6 0 003 0",
  keyboard: "M2.5 5.5h15v9h-15zM5 8.5h1M8 8.5h1M11 8.5h1M14 8.5h1M6 11.5h8",
  rail: "M3 4h14v12H3zM13 4v12",
  activity: "M2 10h4l2-5 4 10 2-5h4",
  database: "M4 5c0-1.1 2.7-2 6-2s6 .9 6 2-2.7 2-6 2-6-.9-6-2zM4 5v10c0 1.1 2.7 2 6 2s6-.9 6-2V5M4 10c0 1.1 2.7 2 6 2s6-.9 6-2",
  chevron: "M7 8l3 3 3-3",
  eyeOff: "M3 3l14 14M8.6 4.7A8 8 0 0110 4.5c5 0 8 5.5 8 5.5a14 14 0 01-2.3 2.9M12.2 15.1a7.6 7.6 0 01-2.2.4c-5 0-8-5.5-8-5.5a14 14 0 013.4-3.8M8.6 8.6a2 2 0 002.8 2.8",
  lock: "M5 9h10v8H5zM7 9V6.5a3 3 0 016 0V9M10 12.5v1.5",
  user: "M10 7m-3 0a3 3 0 106 0a3 3 0 10-6 0M4 17c.8-3 3.2-4.5 6-4.5s5.2 1.5 6 4.5",
  users: "M8 7m-2.5 0a2.5 2.5 0 105 0a2.5 2.5 0 10-5 0M3 16c.6-2.5 2.6-3.8 5-3.8s4.4 1.3 5 3.8M13.5 4.8a2.5 2.5 0 010 4.6M14.5 12.4c1.3.4 2.2 1.6 2.5 3.6",
  key: "M7 13m-3 0a3 3 0 106 0a3 3 0 10-6 0M9.2 10.8L16 4M13.5 6.5l2 2M11.8 8.2l1.5 1.5",
  logout: "M8 4H4v12h4M12 6l4 4-4 4M16 10H8",
};

export function Icon({ name, size = 18, className }: { name: keyof typeof PATHS | string; size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className}>
      <path d={PATHS[name] ?? PATHS.dashboard} />
    </svg>
  );
}
