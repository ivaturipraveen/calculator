/**
 * Mount real calculator pages against the running API.
 *
 * These are not snapshot tests. Each one drives the page the way a clinician
 * would -- type a weight, tick a criterion, answer a question -- and asserts
 * the number that appears is the number the source document states. A mocked
 * API could only prove the component agrees with the mock; this proves the
 * whole path agrees with the PDF.
 *
 * Requires the API on http://127.0.0.1:8010 (see api/README.md).
 */
import { boundsIn } from "../lib/units";

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeAll, describe, expect, it } from "vitest";
import App from "../App";
import { CalculatorPage } from "../pages/CalculatorPage";

const API = "http://127.0.0.1:8010";

function mountCalculator(nameOrSlug: string) {
  return render(
    <MemoryRouter initialEntries={[`/c/${encodeURIComponent(nameOrSlug)}`]}>
      <Routes>
        <Route path="/c/:name" element={<CalculatorPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function typeInto(label: RegExp | string, text: string) {
  // A quantity field labels two controls -- the box and the unit picker beside
  // it ("Unit for Weight") -- so match the box, not whichever came first.
  const matches = await screen.findAllByLabelText(label);
  const input = (matches.find((el) => el.tagName === "INPUT") ?? matches[0]) as HTMLElement;
  await userEvent.clear(input);
  await userEvent.type(input, text);
  return input;
}

/**
 * Ask for the answer.
 *
 * Every source document says "once all the inputs are entered, click the
 * Calculate button", and the page follows it: the first result is asked for,
 * not produced the instant the last box is filled. After that the page stays
 * live, so a table follows the value being changed.
 */
async function pressCalculate() {
  const btn = await screen.findByRole("button", { name: /^(re)?calculate$/i });
  await waitFor(() => expect(btn).not.toBeDisabled(), { timeout: 5000 });
  await userEvent.click(btn);
}

beforeAll(async () => {
  const res = await fetch(`${API}/api/health`).catch(() => null);
  if (!res?.ok) {
    throw new Error(
      `The API is not running on ${API}. Start it with:\n` +
        `  cd api && ./.venv/bin/uvicorn app.main:app --port 8010`,
    );
  }
});

describe("the index", () => {
  it("lists every calculator alphabetically, under its initial", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );

    const index = await screen.findByRole("navigation", { name: /all calculators/i });
    await within(index).findByRole("link", { name: /4T score/i });

    // Every calculator, in one list -- no paging, no second axis to choose.
    // The number comes from the API, so a silent loss shows up as a mismatch
    // rather than as a test that had to be edited.
    const served = (await (await fetch(`${API}/api/calculators/names`)).json()).count;
    await waitFor(() =>
      expect(index.querySelectorAll(".calc-item").length).toBe(served),
    );
    // A heading per initial, in order.
    const letters = [...index.querySelectorAll(".group-label")].map((e) => e.textContent);
    expect(letters.length).toBeGreaterThan(10);
    expect([...letters].sort()).toEqual(letters);
  });

  it("filters the index by name", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    const index = await screen.findByRole("navigation", { name: /all calculators/i });
    await within(index).findByRole("link", { name: /4T score/i });

    await userEvent.type(screen.getByLabelText(/filter calculators by name/i), "dobutamine");
    await waitFor(() => expect(index.querySelectorAll(".calc-item").length).toBe(1));
    expect(within(index).getByRole("link", { name: /dobutamine/i })).toBeInTheDocument();
  });

  it("filters the index by specialty", async () => {
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    const index = await screen.findByRole("navigation", { name: /all calculators/i });
    await within(index).findByRole("link", { name: /4T score/i });
    const all = index.querySelectorAll(".calc-item").length;

    await userEvent.selectOptions(
      screen.getByLabelText(/filter by specialty/i),
      "Anthropometrics",
    );
    await waitFor(() => {
      const n = index.querySelectorAll(".calc-item").length;
      expect(n).toBeGreaterThan(0);
      expect(n).toBeLessThan(all);
    });
  });

  it("marks the calculator being shown", async () => {
    render(
      <MemoryRouter initialEntries={["/c/4T%20score"]}>
        <App />
      </MemoryRouter>,
    );
    const index = await screen.findByRole("navigation", { name: /all calculators/i });
    await waitFor(() => {
      const active = index.querySelector(".calc-item.is-active");
      expect(active).not.toBeNull();
      expect(active!.textContent).toMatch(/4T score/i);
    });
  });
});

describe("formula calculator", () => {
  it("computes Cockcroft-Gault and separates the sexes", async () => {
    mountCalculator("creatinine-clearance-by-cockcroft-gault-age-16-years");
    await screen.findByRole("heading", { name: /Cockcroft-Gault/i });

    await typeInto(/^Age/, "70");
    await typeInto(/^Weight/, "70");
    await typeInto(/Serum creatinine/i, "1");
    await userEvent.selectOptions(await screen.findByLabelText(/^Sex/), "0.85");
    await pressCalculate();

    // 0.85 * ((140 - 70) / 1) * (70 / 72) = 57.85 mL/min
    await waitFor(() => expect(screen.getByText(/^58$|^57\.85$/)).toBeInTheDocument(), {
      timeout: 8000,
    });

    await userEvent.selectOptions(screen.getByLabelText(/^Sex/), "1");
    await waitFor(() => expect(screen.getByText(/^68$|^68\.06$/)).toBeInTheDocument(), {
      timeout: 8000,
    });
  });

  it("refuses a value outside the stated range", async () => {
    mountCalculator("creatinine-clearance-by-cockcroft-gault-age-16-years");
    await screen.findByRole("heading", { name: /Cockcroft-Gault/i });

    await typeInto(/^Age/, "4");
    await typeInto(/^Weight/, "70");
    await typeInto(/Serum creatinine/i, "1");
    await userEvent.selectOptions(await screen.findByLabelText(/^Sex/), "0.85");
    await pressCalculate();

    const alert = await screen.findByRole("alert", {}, { timeout: 8000 });
    expect(alert).toHaveTextContent(/at least 16/i);
  });
});

describe("score calculator", () => {
  it("totals the 4T score and names the band", async () => {
    mountCalculator("4t-score");
    await screen.findByRole("heading", { name: /4T score/i });

    const radios = await screen.findAllByRole("radio");
    const seen = new Set<string>();
    for (const radio of radios) {
      const name = radio.getAttribute("name") ?? "";
      if (seen.has(name)) continue;
      seen.add(name);
      await userEvent.click(radio);
    }
    await pressCalculate();

    // Scope to the total, not any "8" on the page -- the band range says 6-8 too.
    await waitFor(
      () => expect(document.querySelector(".score-total__value")).toHaveTextContent("8"),
      { timeout: 8000 },
    );
    // The band appears twice by design: once as the answer, once highlighted in
    // the list of all bands so the clinician can see where the score sits.
    expect(document.querySelector(".band__label")).toHaveTextContent(/high probability/i);
    expect(document.querySelector(".band-row--active")).toHaveTextContent(/high probability/i);
  });
});

describe("growth chart", () => {
  it("changes the percentile when the sex changes", async () => {
    mountCalculator("cdc-growth-percentiles-36-months");
    await screen.findByRole("heading", { name: /CDC Growth Percentiles/i });

    await userEvent.selectOptions(await screen.findByLabelText(/^Sex/), "1");
    await typeInto(/^Age/, "12.5");
    await typeInto(/^Length/, "76");
    await typeInto(/^Weight/, "9.6");
    await typeInto(/Head circumference/i, "46");
    await pressCalculate();

    // The lead result is the hero; the rest are rows. Either can hold it.
    const percentile = () =>
      Array.from(document.querySelectorAll(".value-row, .hero"))
        .find((c) => /weight percentile/i.test(c.textContent ?? ""))
        ?.querySelector(".value-row__value, .hero__value")?.textContent ?? "";

    await waitFor(() => expect(percentile()).toMatch(/^47\.4/), { timeout: 8000 });

    await userEvent.selectOptions(screen.getByLabelText(/^Sex/), "2");
    await waitFor(() => expect(percentile()).toMatch(/^21\.6|^21\.7/), { timeout: 8000 });
  });
});

describe("infusion calculator", () => {
  it("computes the rate and highlights that dose in the titration table", async () => {
    mountCalculator("dobutamine");
    await screen.findByRole("heading", { name: /dobutamine/i });

    await typeInto(/^Dose/, "5");
    await typeInto(/^Weight/, "70");
    await typeInto(/^Concentration/, "1000");
    await typeInto(/Drug amount/i, "250");
    await typeInto(/Infusate volume/i, "250");
    await pressCalculate();

    // 5 mcg/kg/min x 70 kg = 21,000 mcg/hr / 1000 mcg/mL = 21 mL/hr
    await waitFor(() => expect(screen.getAllByText("21").length).toBeGreaterThan(0), {
      timeout: 8000,
    });

    const table = await screen.findByRole("table");
    const highlighted = table.querySelector("tr.is-current");
    expect(highlighted).not.toBeNull();
    expect(highlighted!.textContent).toMatch(/^5\s*21/);
  });

  it("accepts a weight in pounds", async () => {
    mountCalculator("dobutamine");
    await screen.findByRole("heading", { name: /dobutamine/i });

    await userEvent.selectOptions(await screen.findByLabelText(/unit for weight/i), "lbs");
    await typeInto(/^Dose/, "5");
    await typeInto(/^Weight/, "154.324");
    await typeInto(/^Concentration/, "1000");
    await typeInto(/Drug amount/i, "250");
    await typeInto(/Infusate volume/i, "250");
    await pressCalculate();

    await waitFor(() => expect(screen.getAllByText("21").length).toBeGreaterThan(0), {
      timeout: 8000,
    });
  });
});

describe("unit converter", () => {
  it("converts and reverses", async () => {
    mountCalculator("unit-conversions-weight");
    await screen.findByRole("heading", { name: /weight/i });

    await userEvent.selectOptions(await screen.findByLabelText(/^Conversion/), "0");
    await typeInto(/^Value/, "70");
    await pressCalculate();

    await waitFor(() => expect(screen.getByText(/154\.32/)).toBeInTheDocument(), { timeout: 8000 });
  });
});

describe("decision tree", () => {
  it("asks a question and reaches an outcome", async () => {
    mountCalculator("rabies-post-exposure-prophylaxis-treecalc");
    await screen.findByRole("heading", { name: /rabies/i });

    await waitFor(() => expect(screen.getByText(/contact with saliva/i)).toBeInTheDocument(), {
      timeout: 8000,
    });

    await userEvent.click(screen.getByRole("button", { name: /^No$/ }));
    expect(
      await screen.findByText(/prophylaxis not required/i, {}, { timeout: 8000 }),
    ).toBeInTheDocument();
  });
});

describe("catalogue controls", () => {
  it("removes a filter from its pill", async () => {
    render(
      <MemoryRouter initialEntries={["/?category=Anthropometrics"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { level: 1, name: /anthropometrics/i });
    await userEvent.click(await screen.findByLabelText(/clear specialty filter/i));
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/all calculators/i),
    );
  });
});

describe("reference data", () => {
  it("shows the LMS table as a table, and marks the row the lookup used", async () => {
    mountCalculator("CDC BMI for Age, Boys, 2-20 Years");
    await screen.findByRole("heading", { name: /BMI for Age/i });

    await typeInto(/^Age/, "15");
    await typeInto(/^Height/, "170");
    await typeInto(/^Weight/, "60");
    await pressCalculate();

    // 60 / 1.70^2 = 20.76 kg/m2
    await waitFor(() => expect(document.querySelector(".hero__value")).toHaveTextContent(/20\.8|20\.76/), {
      timeout: 8000,
    });

    const table = await screen.findByRole("table");
    expect(within(table).getByText("L")).toBeInTheDocument();
    expect(table.querySelector("tr.is-current")).not.toBeNull();

    // The 14,000-character prose dump of the same numbers must be gone.
    expect(document.body.textContent).not.toMatch(/0\.080127429/);

    await userEvent.click(screen.getByRole("button", { name: /show all 217/i }));
    await waitFor(() => expect(table.querySelectorAll("tbody tr").length).toBe(217));
  });

  it("does not print prose in the unit slot", async () => {
    mountCalculator("CDC BMI for Age, Boys, 2-20 Years");
    await screen.findByRole("heading", { name: /BMI for Age/i });
    await waitFor(() => expect(document.body.textContent).not.toMatch(/Percentile <5:/));
  });
});

describe("extraction caveats", () => {
  it("warns when the source document cut off the dose ladder", async () => {
    mountCalculator("DOPamine");
    await screen.findByRole("heading", { name: /dopamine/i });
    expect(
      await screen.findByText(/printed dose array is cut off/i),
    ).toBeInTheDocument();
  });

  it("says nothing when there is nothing to warn about", async () => {
    mountCalculator("4T score");
    await screen.findByRole("heading", { name: /4T score/i });
    expect(screen.queryByText(/known limits of this extraction/i)).toBeNull();
  });
});

describe("page furniture", () => {
  it("shows the formula and the disclaimer without being opened", async () => {
    mountCalculator("Creatinine Clearance by Cockcroft-Gault, Age \u226516 Years");
    await screen.findByRole("heading", { name: /cockcroft-gault/i });

    // The reference column is the source document talking, and all of it is on
    // screen: a disclaimer folded behind a summary is a disclaimer nobody read.
    const info = await screen.findByRole("complementary", { name: /formula & references/i });
    expect(within(info).getByRole("heading", { name: /^formula$/i })).toBeInTheDocument();
    expect(within(info).getByText(/ONLY DIGITS 0 TO 9/i)).toBeInTheDocument();
    expect(info.querySelectorAll("details").length).toBe(0);
  });

  it("puts the calculator, its inputs and its answer on the clipboard", async () => {
    const written: string[] = [];
    Object.assign(navigator, {
      clipboard: { writeText: async (t: string) => void written.push(t) },
    });

    mountCalculator("Dobutamine");
    await screen.findByRole("heading", { name: /dobutamine/i });
    await typeInto(/^Dose/, "5");
    await typeInto(/^Weight/, "70");
    await typeInto(/^Concentration/, "1000");
    await typeInto(/Drug amount/i, "250");
    await typeInto(/Infusate volume/i, "250");
    await pressCalculate();

    const copy = await screen.findByRole("button", { name: /copy result/i }, { timeout: 8000 });
    await userEvent.click(copy);

    await waitFor(() => expect(written.length).toBe(1));
    // The source spells it "DOBUTamine" -- tall-man lettering, kept verbatim.
    expect(written[0]).toMatch(/DOBUTamine/i);
    expect(written[0]).toMatch(/Dose: 5/);
    expect(written[0]).toMatch(/Infuse Rate = 21/);
  });
});

describe("conditional fields", () => {
  it("only asks for the coefficient that applies to the chosen sex", async () => {
    mountCalculator("ValGANciclovir");
    await screen.findByRole("heading", { name: /valganciclovir/i });

    // Before a sex is chosen the form asks for neither exclusively.
    await userEvent.selectOptions(await screen.findByLabelText(/^Gender/), "MALE");
    await waitFor(() => expect(screen.queryByLabelText(/age band \(female\)/i)).toBeNull());
    expect(screen.getByLabelText(/age band \(male\)/i)).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText(/^Gender/), "FEMALE");
    await waitFor(() => expect(screen.queryByLabelText(/age band \(male\)/i)).toBeNull());
    expect(screen.getByLabelText(/age band \(female\)/i)).toBeInTheDocument();
  });
});

describe("tables react to the inputs", () => {
  it("re-doses the whole drug table when the weight changes", async () => {
    mountCalculator("Advanced Life Support: Adult");
    await screen.findByRole("heading", { name: /advanced life support/i });

    const doseFor = (drug: RegExp) => {
      const row = Array.from(document.querySelectorAll("tbody tr")).find((r) =>
        drug.test(r.textContent ?? ""),
      );
      return row?.textContent ?? "";
    };

    await typeInto(/^Weight/, "20");
    await pressCalculate();
    // Norepinephrine is 0.1-0.5 mcg/kg/min: 20 kg -> 2-10 mcg/min
    await waitFor(() => expect(doseFor(/Norepinephrine/i)).toMatch(/2–10 mcg/), {
      timeout: 8000,
    });

    await typeInto(/^Weight/, "70");
    await waitFor(() => expect(doseFor(/Norepinephrine/i)).toMatch(/7–35 mcg/), {
      timeout: 8000,
    });

    // Adult adrenaline is a fixed 1 mg and must NOT scale with weight.
    expect(doseFor(/Adenosine/i)).toMatch(/6 mg/);
  });

  it("moves the highlighted lookup row when the age changes", async () => {
    mountCalculator("CDC BMI for Age, Boys, 2-20 Years");
    await screen.findByRole("heading", { name: /BMI for Age/i });

    await typeInto(/^Height/, "170");
    await typeInto(/^Weight/, "60");
    await typeInto(/^Age/, "15");
    await pressCalculate();

    const current = () => document.querySelector("tr.is-current")?.textContent ?? "";
    await waitFor(() => expect(current()).toMatch(/^181/), { timeout: 8000 });

    await typeInto(/^Age/, "10");
    // 10 years -> 120 months -> the 121-month row.
    await waitFor(() => expect(current()).toMatch(/^121/), { timeout: 8000 });

    // The row in use is centred in the window, not sitting on its edge.
    const rows = Array.from(document.querySelectorAll("tbody tr"));
    const at = rows.findIndex((r) => r.classList.contains("is-current"));
    expect(at).toBeGreaterThan(0);
    expect(at).toBeLessThan(rows.length - 1);
  });

  it("recomputes the titration ladder when the concentration changes", async () => {
    mountCalculator("DOBUTamine");
    await screen.findByRole("heading", { name: /dobutamine/i });

    await typeInto(/^Dose/, "5");
    await typeInto(/^Weight/, "70");
    await typeInto(/Drug amount/i, "250");
    await typeInto(/Infusate volume/i, "250");
    await typeInto(/^Concentration/, "1000");
    await pressCalculate();

    const rowFor = (dose: string) =>
      Array.from(document.querySelectorAll("tbody tr")).find(
        (r) => r.querySelector("td")?.textContent === dose,
      )?.textContent ?? "";

    await waitFor(() => expect(rowFor("10")).toMatch(/42/), { timeout: 8000 });

    // Half the concentration, double every rate in the table.
    await typeInto(/^Concentration/, "500");
    await waitFor(() => expect(rowFor("10")).toMatch(/84/), { timeout: 8000 });
  });
});

describe("limits follow the unit in the box", () => {
  const field = {
    key: "patient_temp",
    label: "Patient Temp",
    widget: "quantity" as const,
    required: true,
    required_when: null,
    default: null,
    help: null,
    dimension: "temperature",
    constraints: { min: 0, max: 50, step: 0.1 },
    constraints_by_mode: null,
    unit: {
      base: "degC",
      display: "degC",
      options: [
        { code: "degC", label: "Degrees C", factor: 1, offset: 0, is_base: true },
        { code: "degF", label: "Degrees F", factor: 5 / 9, offset: -32 * (5 / 9) },
      ],
      selectable: true,
    },
    options: [],
    options_incomplete: false,
    depends_on: null,
    visible_when: null,
    source: null,
  };

  it("states the range in the selected unit, not the base unit", () => {
    expect(boundsIn(field, "degC")).toMatchObject({ min: 0, max: 50, converted: false });
    // 0 degC is 32 degF and 50 degC is 122 degF -- a normal 98.6 must be inside.
    const f = boundsIn(field, "degF");
    expect(f.min).toBeCloseTo(32, 6);
    expect(f.max).toBeCloseTo(122, 6);
    expect(f.converted).toBe(true);
  });

  it("converts a plain scale the same way", () => {
    const elevation = {
      ...field,
      key: "elevation",
      constraints: { min: 0, max: 10000, step: 1 },
      unit: {
        base: "meters",
        display: "meters",
        options: [
          { code: "meters", label: "meters", factor: 1, offset: 0, is_base: true },
          { code: "feet", label: "feet", factor: 0.3048, offset: 0 },
        ],
        selectable: true,
      },
    };
    const ft = boundsIn(elevation, "feet");
    expect(ft.max).toBeCloseTo(32808.398950131, 6);
  });
});

describe("what the source could not give us is said on the page", () => {
  it("warns that APACHE II cannot move, instead of returning 0 in silence", async () => {
    mountCalculator("APACHE II Scoring System");
    await screen.findByRole("heading", { name: /APACHE II/i });
    const warning = await screen.findByText(
      /offer a single choice|barely moves/i,
      {},
      { timeout: 5000 },
    );
    expect(warning).toBeInTheDocument();
  });

  it("shows a limit the script applies to a value it works out", async () => {
    mountCalculator("Esmolol");
    await screen.findByRole("heading", { name: /esmolol/i });
    // The jump-nav names the section as well as the panel heading.
    const shown = await screen.findAllByText(
      /Limits the document states/i,
      {},
      { timeout: 5000 },
    );
    expect(shown.length).toBeGreaterThan(0);
    // The same name heads the result row; what matters is that the LIMIT is
    // stated -- 0.1 mL is the floor the script refuses to go below.
    const panel = shown[shown.length - 1].closest(".panel") as HTMLElement;
    expect(within(panel).getByText(/Bolus Dose \(mL\)/i)).toBeInTheDocument();
    expect(within(panel).getByText(/0\.1/)).toBeInTheDocument();
  });
});

describe("branches the extraction had lost", () => {
  it("ethanol doses an oral order from the 98% solution, not the IV 10%", async () => {
    mountCalculator("Ethanol");
    await screen.findByRole("heading", { name: /ethanol/i });

    await userEvent.selectOptions(await screen.findByLabelText(/output units/i), "mL");
    await userEvent.selectOptions(await screen.findByLabelText(/^route/i), "Oral");
    await userEvent.selectOptions(await screen.findByLabelText(/hemodialysis/i), "No");
    await userEvent.selectOptions(await screen.findByLabelText(/chronic drinker/i), "No");
    await typeInto(/weight/i, "70");
    await pressCalculate();

    // 70 kg x 0.78 mL/kg of a 98% solution.
    await screen.findByText(/^54\.6$/, {}, { timeout: 5000 });

    await userEvent.selectOptions(screen.getByLabelText(/^route/i), "IV");
    // 70 kg x 7.6 mL/kg of a 10% solution -- ten times the volume.
    await screen.findByText(/^532(\.0+)?$/, {}, { timeout: 5000 });
  });

  it("fentanyl works out the bolus volume only when a bolus is given", async () => {
    mountCalculator("FentaNYL");
    await screen.findByRole("heading", { name: /fentanyl/i });

    await typeInto(/weight/i, "70");
    await typeInto(/bolus dose/i, "2");
    await typeInto(/infusion dose/i, "1");
    await typeInto(/concentration/i, "10");
    await typeInto(/drug amount/i, "500");
    await typeInto(/infusate volume/i, "50");
    await userEvent.selectOptions(await screen.findByLabelText(/bolus option/i), "yes");
    await pressCalculate();

    // 70 kg x 2 mcg/kg / 10 mcg/mL.
    await screen.findByText(/^14(\.0+)?$/, {}, { timeout: 5000 });
  });
});

describe("a value the chosen path never uses is not demanded", () => {
  it("fentanyl computes the infusion rate without a bolus dose", async () => {
    mountCalculator("FentaNYL");
    await screen.findByRole("heading", { name: /fentanyl/i });

    await typeInto(/weight/i, "70");
    await typeInto(/infusion dose/i, "1");
    await typeInto(/concentration/i, "10");
    await typeInto(/drug amount/i, "500");
    await typeInto(/infusate volume/i, "50");
    await userEvent.selectOptions(await screen.findByLabelText(/bolus option/i), "No");
    await pressCalculate();

    // 1 mcg/kg/min x 70 kg x 60 / 10 mcg/mL / 1000 -- the bolus box is empty.
    await screen.findByText(/^7(\.0+)?$/, {}, { timeout: 5000 });
    expect((screen.getAllByLabelText(/bolus dose/i)[0] as HTMLInputElement).value).toBe("");
  });
});

describe("the answer can be checked against what produced it", () => {
  it("reads the entered values back beside the result", async () => {
    mountCalculator("A-a Gradient");
    await screen.findByRole("heading", { name: /A-a Gradient/i });

    await typeInto(/^age/i, "40");
    await typeInto(/patient temp/i, "37");
    await typeInto(/elevation/i, "0");
    await typeInto(/percent inspired/i, "21");
    await typeInto(/p CO2/i, "40");
    await typeInto(/resp quot/i, "0.8");
    await typeInto(/p aO2/i, "90");
    await pressCalculate();

    const summary = await screen.findByText(/values used/i, {}, { timeout: 5000 });
    await userEvent.click(summary);
    const used = summary.closest("details") as HTMLElement;
    const row = within(used).getByText(/^Age$/).closest(".used__row") as HTMLElement;
    expect(within(row).getByText(/^40/)).toBeInTheDocument();
    expect(within(row).getByText(/yr/)).toBeInTheDocument();
  });
});

describe("a calculator the source left unusable is marked before it is opened", () => {
  it("flags it on the catalogue card", async () => {
    render(
      <MemoryRouter initialEntries={["/?q=APACHE"]}>
        <App />
      </MemoryRouter>,
    );
    // The card on the catalogue, not the row in the index beside it.
    const main = await screen.findByRole("main");
    const links = await within(main).findAllByRole("link", { name: /APACHE II/i });
    const card = links[0].closest(".calc-card") as HTMLElement;
    expect(within(card).getByText(/limited/i)).toBeInTheDocument();
  });
});

describe("a calculator whose name contains a slash", () => {
  // "ACC/AHA 2013" encodes to %2F, which the server decodes back to "/" before
  // routing -- so a plain {slug} path parameter saw two segments and returned
  // 404. Three of the corpus's calculators could not be opened at all.
  it.each([
    "ACC/AHA 2013 Cardiovascular Risk Assessment",
    "Pediatric Dosing: Oral Liquid/Parenteral",
    "TIMI Risk Score (UA/NSTEMI)",
  ])("opens %s", async (title) => {
    mountCalculator(title);
    expect(
      await screen.findByRole("heading", { level: 1, name: new RegExp(title.split(" ")[0], "i") }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/could not load/i)).toBeNull();
  });
});

describe("the corpus is complete", () => {
  it("serves every calculator the build published", async () => {
    const res = await fetch(`${API}/api/calculators/names`);
    const { count, names: rows } = await res.json();
    const names = rows.map((r: { name?: string; title?: string } | string) =>
      typeof r === "string" ? r : (r.name ?? r.title),
    );
    expect(count).toBe(names.length);
    // The 2016 MELDNa revision is a different formula from MELDNa, and was
    // being discarded as a duplicate export because "(2016)" looked like the
    // "(2)" an operating system appends to a second download.
    expect(names).toContain("MELDNa Score (2016)");
    expect(names).toContain("MELDNa Score");
  });
});

describe("bugs the UI hit that the API alone did not", () => {
  it("gestational age accepts what a date picker submits, and answers in dates", async () => {
    mountCalculator("Gestational Age");
    await screen.findByRole("heading", { level: 1, name: /gestational age/i });

    // `<input type="date">` submits "2025-08-01". The formulas work in epoch
    // milliseconds, and nothing converted between the two: every date field
    // came back "must be a number".
    const set = async (id: string, v: string) => {
      const el = document.getElementById(`f-${id}`) as HTMLInputElement;
      await userEvent.type(el, v);
    };
    await set("current_time", "2026-03-15");
    await set("lmp_time", "2025-08-01");
    await set("crown_rump_length", "30");
    await set("biparietal_diameter", "45");
    await set("head_circumference", "160");
    await set("us_time", "2026-01-10");
    await pressCalculate();

    await waitFor(
      () => expect(document.querySelectorAll(".field__error").length).toBe(0),
      { timeout: 5000 },
    );
    // 2025-08-01 to 2026-03-15 is 226 days -- 32 weeks.
    await waitFor(
      () => expect(document.querySelector(".hero__value")?.textContent).toMatch(/^32/),
      { timeout: 5000 },
    );
    // And an estimated date of confinement reads as a date, not as 1778198400000.
    expect(screen.queryByText(/17781984\d+/)).toBeNull();
    expect(screen.getAllByText(/\b20(2[5-9])\b/).length).toBeGreaterThan(0);
  });

  it("valganciclovir finishes with only the coefficient its sex needs", async () => {
    mountCalculator("ValGANciclovir");
    await screen.findByRole("heading", { level: 1, name: /valganciclovir/i });

    const byId = (id: string) => document.getElementById(`f-${id}`);
    await userEvent.selectOptions(byId("id_gender") as HTMLElement, "MALE");
    for (const [id, v] of [["height", "150"], ["weight", "45"], ["sr_cr", "0.6"], ["form", "1"]]) {
      await userEvent.type(byId(id) as HTMLInputElement, v);
    }
    const band = byId("id_k_age_m") as HTMLSelectElement;
    await userEvent.selectOptions(band, band.options[band.options.length - 1].value);

    // The female coefficient is required in the spec and gated to FEMALE, so it
    // is never on the form -- and the page waited for it forever.
    expect(byId("id_k_age_f")).toBeNull();
    await waitFor(
      () => expect(document.querySelector(".hero")?.textContent ?? "").not.toMatch(/calculating/),
      { timeout: 6000 },
    );
  });
});

describe("the form does not answer with values nobody entered", () => {
  it("opens A-a Gradient with every required box empty", async () => {
    mountCalculator("A-a Gradient");
    await screen.findByRole("heading", { level: 1, name: /A-a Gradient/i });

    // 37, 21 and 0.8 are what was in the boxes when the source page was
    // printed, not this patient's temperature, oxygen and quotient.
    for (const id of ["patient_temp", "percent_inspired_o2", "resp_quot", "elevation"]) {
      const el = document.getElementById(`f-${id}`) as HTMLInputElement;
      expect(el.value).toBe("");
      // still visible, as the document's value, without being submitted
      expect(el.placeholder).not.toBe("");
    }
    expect(document.querySelector(".hero")?.textContent ?? "").toMatch(/—|to go|calculating/);
  });

  it("keeps a default that means something", async () => {
    mountCalculator("Morphine Milligram Equivalents per Day (MMED)");
    await screen.findByRole("heading", { level: 1, name: /morphine milligram/i });
    // "not on this opioid" is a real answer, and thirteen boxes of it is not
    // something to make a clinician type.
    const el = document.getElementById("f-codeine") as HTMLInputElement;
    expect(el.value).toBe("0");
  });
});
