// A compact row of numeric stat tiles (value + label). Used for app composition
// summaries (agents, built-in endpoints, supporting functions, etc.).

export interface StatTile {
  n: number | string
  label: string
}

export const StatTiles = ({ items }: { items: StatTile[] }) => (
  <div className="stat-row">
    {items.map((s) => (
      <div className="stat" key={s.label}>
        <span className="n">{s.n}</span>
        <span className="l">{s.label}</span>
      </div>
    ))}
  </div>
)
