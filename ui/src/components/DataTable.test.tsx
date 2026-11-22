import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { compareValues, DataTable, sortRows, type Column } from "./DataTable";

interface Row {
  id: string;
  name: string;
  size: number | null;
}

const rows: Row[] = [
  { id: "a", name: "file10", size: 300 },
  { id: "b", name: "file2", size: null },
  { id: "c", name: "File1", size: 20 },
  { id: "d", name: "file3", size: 1000 },
];

const columns: Column<Row>[] = [
  { key: "name", header: "Name" },
  { key: "size", header: "Size", align: "right" },
  { key: "id", header: "ID", sortable: false },
];

function bodyOrder(): string[] {
  const body = screen.getAllByRole("rowgroup")[1]!;
  return within(body)
    .getAllByRole("row")
    .map((row) => row.getAttribute("data-key") ?? "");
}

describe("DataTable", () => {
  it("keeps the incoming order until a header is clicked", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    expect(bodyOrder()).toEqual(["a", "b", "c", "d"]);
  });

  it("sorts numerically ascending then descending, empty values last both ways", async () => {
    const user = userEvent.setup();
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const sizeHeader = screen.getByRole("columnheader", { name: /size/i });

    await user.click(within(sizeHeader).getByRole("button"));
    expect(sizeHeader).toHaveAttribute("aria-sort", "ascending");
    expect(bodyOrder()).toEqual(["c", "a", "d", "b"]);

    await user.click(within(sizeHeader).getByRole("button"));
    expect(sizeHeader).toHaveAttribute("aria-sort", "descending");
    expect(bodyOrder()).toEqual(["d", "a", "c", "b"]);
  });

  it("sorts strings with natural, case-insensitive ordering", async () => {
    const user = userEvent.setup();
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    await user.click(screen.getByRole("button", { name: /name/i }));
    expect(bodyOrder()).toEqual(["c", "b", "d", "a"]); // File1, file2, file3, file10
  });

  it("does not offer sorting on non-sortable columns", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const idHeader = screen.getByRole("columnheader", { name: "ID" });
    expect(within(idHeader).queryByRole("button")).toBeNull();
    expect(idHeader).not.toHaveAttribute("aria-sort");
  });

  it("reports sort changes and row clicks", async () => {
    const user = userEvent.setup();
    const onSortChange = vi.fn();
    const onRowClick = vi.fn();
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} onSortChange={onSortChange} onRowClick={onRowClick} />);
    await user.click(screen.getByRole("button", { name: /name/i }));
    expect(onSortChange).toHaveBeenCalledWith({ key: "name", direction: "asc" });
    await user.click(screen.getByText("file3"));
    expect(onRowClick).toHaveBeenCalledWith(rows[3]);
  });

  it("renders the empty state when there are no rows", () => {
    render(<DataTable columns={columns} rows={[]} rowKey={(r) => r.id} empty={<span>nothing here</span>} />);
    expect(screen.getByText("nothing here")).toBeInTheDocument();
  });
});

describe("sorting helpers", () => {
  it("compareValues orders numbers, dates and strings", () => {
    expect(compareValues(2, 10)).toBeLessThan(0);
    expect(compareValues("a2", "a10")).toBeLessThan(0);
    expect(compareValues(new Date(2020, 0, 1), new Date(2021, 0, 1))).toBeLessThan(0);
    expect(compareValues(1, null)).toBeLessThan(0);
  });

  it("sortRows is stable and does not mutate the input", () => {
    const input = [
      { id: "x", name: "same", size: 1 },
      { id: "y", name: "same", size: 2 },
    ];
    const out = sortRows(input, columns, { key: "name", direction: "desc" });
    expect(out.map((r) => r.id)).toEqual(["x", "y"]);
    expect(out).not.toBe(input);
  });
});
