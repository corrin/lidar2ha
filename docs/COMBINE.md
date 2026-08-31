# Combining captures

`combine` answers two different questions in order:

1. Where does each capture sit relative to the others?
2. Which capture supplies the geometry for each area?

Mixing those questions is how a small capture of new ground lands on the wrong
room. A fit that explains every wall by putting a basement scan on top of a den
can score better than the correct fit, because the correct fit necessarily
leaves the new basement unexplained.

## 1. Prepare each capture

Import the DXF and CSV, register the plan against its own mesh, and run `rooms`
before combining. Scanner labels are observations, not identity. A value of
`null` in `rooms:` is a declared unknown and stays unknown.

Every input room must leave the stage with a disposition: selected, a losing
candidate, alignment context, unseen, unresolved, malformed, or excluded with
its capture. Nothing is omitted merely because it could not become output
geometry.

## 2. Place captures on common ground

The fitter generates placement hypotheses from the four rotations admitted by
the wall grid and from plausible room correspondences. A declared shared area
is identity evidence; a similar area or outline is only a hypothesis.

Each placement is graded on the ground the two captures share. Walls outside
that common ground may be a newly scanned room, so they neither reward nor
penalise the placement. Capture-wide coverage is reported and never used as a
veto.

Measured placement has three outcomes:

- **placed** -- one hypothesis is supported;
- **ambiguous** -- more than one hypothesis remains plausible;
- **unplaceable** -- none is supported.

Ambiguous and unplaceable captures contribute no geometry. Their hypotheses
and measurements remain in the alignment record.

Adjacent inside and outside scans may share no floor or walls at all. In that
case fitting has no evidence to work with. Two corresponding point pairs can
declare the rigid join in `project.yaml`:

```yaml
placements:
  Ground Floor:
    - capture: deck
      relative_to: inside
      capture_points_cm: [[1120, 430], [1220, 430]]
      relative_points_cm: [[615, 870], [715, 870]]
      evidence: aligned the two ends of the shared door sill
```

The points are plan centimetres in each capture's own frame. Their direction
determines rotation and their midpoint determines translation. The declaration
is binding geometry, with its point-pair residual and evidence recorded. It is
never relabelled as measured wall overlap: `median_error_m`, coverage and p90
remain absent and the alignment JSON says `measured_overlap: null`.

A declaration may attach to a measured capture or to another declared capture,
forming an explicit path into the reference frame. The reference itself must be
a measured root. Unknown captures, duplicate declarations, self-reference,
cycles and unresolved paths fail at the boundary. If a declaration's target is
present but its measured placement is refused, the dependent capture is also
refused and named in the report.

## 3. Build the area observations

Once captures share a frame, each `(capture, area)` observation has one of
three states:

- **candidate** -- sufficiently complete to supply the area's geometry;
- **context** -- enough was seen to place the capture, but not enough to
  replace an existing survey of the area;
- **unseen** -- the capture did not observe the area.

This is the ordinary incremental workflow: scan part of a known bedroom for
context, then scan the missing basement properly. The bedroom observation may
place the capture without competing to replace the bedroom; the basement is a
new candidate.

An unnamed room cannot silently replace a named area. Its overlap is reported
as a naming suggestion until a person declares its identity.

## 4. Form a leave-one-out mean for each area

Candidate outlines and their supporting walls are sampled at a configurable
spacing. To score one candidate, form the area's mean from the *other*
candidate captures. A capture never votes on the reference used to grade it.

Measure both directions:

- candidate to mean measures accuracy;
- mean to candidate measures completeness.

The mean is a scoring reference only. Output geometry is always selected whole
from a real capture; it is never the averaged outline.

## 5. Select each area independently

Context and unseen observations cannot win. With three or more candidates, the
complete candidate closest to its leave-one-out mean wins. Two materially
disagreeing candidates cannot identify which is right and remain ambiguous. A
single candidate is retained as the best available geometry and marked
single-source and provisional.

Ceiling plausibility, enclosure and wall support are eligibility findings or
named cautions. They do not enter an opaque weighted score that can overrule
distance from the area consensus.

The winner supplies the room and its supporting walls. Doors and detected
features union across placed captures and are deduplicated according to their
own geometry.

## 6. Validate the assembled level

Independently selected areas may reveal a material overlap or uncovered floor.
Both are reported. `combine` does not trim, fill, or switch winners silently.
Floor seen by a placed capture but absent from the selected rooms is attributed
back to the source rooms that saw it.

All behaviour-changing distances, completeness bounds and ambiguity margins
are guesses. They are command-line options, their help names the evidence for
the default, and new measurements are what change them.

## The 2026-08-29 regression

The private house capture has 15 walls and three CSV rooms: a 13.5 m2 bedroom,
an 8.9 m2 room labelled `Living Room`, and a 0.6 m2 remainder. Its own mesh
registration is 6.7 cm at 100% coverage. Registration finds two plausible
quarter-turn basins. Both put the bedroom almost entirely on the declared spare
bedroom, so that declaration alone does not distinguish them. One basin reads
about 2.7 cm at 100% coverage against the established ground model; the other
reads about 4.2 cm at 85% coverage.

Those measurements prove neither `basement` nor `deck` as the identity of the
new room. The correct outcome is an ambiguous placement until another declared
common area or an explicit placement decision breaks the tie. The regression
tests use a reduced, declared-context example to prove the ordering above; they
do not turn a hypothesis about this private room into data.
