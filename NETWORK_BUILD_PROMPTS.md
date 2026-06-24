# Network Build Prompts

Reusable instructions for building a PowerFactory network from an SLD PDF,
running a load flow, and drawing the diagram — in that order. Copy the relevant
prompt into the chat and run it.

The phrase "once the load flow converges, draw the diagram" guarantees the
diagram is only drawn after the build and load flow succeed.

---

## Standard template

> Build the `[network_name]` network from the SLD at `[pdf_path]`, using project
> name `[project_name]`. Once the load flow converges, draw the diagram using the
> Diagram Layout tool.

---

## IEEE 14-bus

> Build the `IEEE_14_Bus` network from the SLD at
> `C:\Users\z004z29x\PycharmProjects\mcp-powerfactory\IEEE_14_Bus_Single_Line_Diagram.pdf`,
> using project name `IEEE_14_Bus_SLD`. Once the load flow converges, draw the
> diagram using the Diagram Layout tool.

---

## IEEE 39-bus

> Build the `IEEE_39_Bus` network from the SLD at
> `C:\Users\z004z29x\PycharmProjects\mcp-powerfactory\IEEE_39_Bus_Single_Line_Diagram.pdf`,
> using project name `IEEE_39_Bus_SLD`. Once the load flow converges, draw the
> diagram using the Diagram Layout tool.

---

## Blank template (fill in for any new network)

> Build the `____________` network from the SLD at
> `____________________________________________`,
> using project name `____________`. Once the load flow converges, draw the
> diagram using the Diagram Layout tool.

| Placeholder | Meaning |
|---|---|
| `network_name` | Name of the grid (ElmNet) to create, e.g. `IEEE_14_Bus` |
| `pdf_path` | Absolute path to the SLD PDF |
| `project_name` | Name of the fresh PowerFactory project to create (replaces any existing project of that name) |
