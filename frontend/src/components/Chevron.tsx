/**
 * A chevron, drawn rather than typed.
 *
 * `◀` and `▶` are solid triangles: at the size these buttons need they read as
 * heavy blocks, and they vary by font. A stroked path is sharp at any size and
 * looks the same everywhere.
 */
export function Chevron({
  dir,
  size = 18,
}: {
  dir: "left" | "right" | "down";
  size?: number;
}) {
  const d =
    dir === "left" ? "M14 5 L8 11 L14 17" : dir === "right" ? "M8 5 L14 11 L8 17" : "M5 8 L11 14 L17 8";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 22 22"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={d} />
    </svg>
  );
}
