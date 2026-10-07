import type { SVGProps } from "react";

const STROKE: SVGProps<SVGSVGElement> = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
};

/** The panel's icon, the same drawing as `assets/bus-monitor.svg`: two bus lines and three nodes on them. */
export const BusMonitorIcon = () => (
  <svg {...STROKE}>
    <path d="M2 10h20" />
    <path d="M2 14h20" />
    <rect x="4" y="3" width="4" height="4" rx="1" />
    <path d="M6 7v3" />
    <rect x="16" y="3" width="4" height="4" rx="1" />
    <path d="M18 7v3" />
    <rect x="10" y="17" width="4" height="4" rx="1" />
    <path d="M12 14v3" />
  </svg>
);

export const SearchIcon = () => (
  <svg {...STROKE}>
    <circle cx="11" cy="11" r="8" />
    <path d="m21 21-4.3-4.3" />
  </svg>
);

export const ClearIcon = () => (
  <svg {...STROKE}>
    <path d="M18 6 6 18" />
    <path d="m6 6 12 12" />
  </svg>
);

export const AlertIcon = () => (
  <svg {...STROKE}>
    <circle cx="12" cy="12" r="10" />
    <line x1="12" x2="12" y1="8" y2="12" />
    <line x1="12" x2="12.01" y1="16" y2="16" />
  </svg>
);
