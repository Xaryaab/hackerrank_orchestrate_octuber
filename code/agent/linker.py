"""Case -> resource linking.

Two kinds of links:
  * direct   - an observation (or a data-catalog activity) mentions one of the subject's
               current/historical identifiers (email, phone, email hash, user_id).
  * workflow - the deletion worker touched a resource while processing this case
               (activities clustered around the case's `reported_at`).  Needed because many
               records carry no identifier at all (already-anonymized analytics, 404 lookups).

Workflow links are resolved globally across all cases (samples + evaluation) because
several cases can share the same `reported_at` batch.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .data import Activity, Dataset, Request, minutes, ts
from .evidence import Observation, resource_prefix
from .identity import IdentityIndex, obs_keys

# reported workflow status <-> reported result of the primary-profile deletion activity
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}

ACCOUNT_WINDOW = (-95.0, -25.0)       # minutes relative to reported_at
RESOURCE_WINDOW = (-45.0, 150.0)


@dataclass
class CaseLinks:
    request: Request
    user_id: str | None
    direct: set = field(default_factory=set)
    workflow: set = field(default_factory=set)
    account: str | None = None
    activities: list = field(default_factory=list)   # workflow activities attributed to the case

    @property
    def resources(self) -> set:
        return self.direct | self.workflow | ({self.account} if self.account else set())


class Linker:
    def __init__(self, ds: Dataset, obs: list[Observation], ids: IdentityIndex):
        self.ds, self.obs, self.ids = ds, obs, ids
        self.owner: dict[str, set] = defaultdict(set)
        self.files_of: dict[str, set] = defaultdict(set)
        for o in obs:
            self.files_of[o.resource_id].add(o.file_id)
            for k in obs_keys(o.attrs):
                for u in ids.by_key.get(k, ()):
                    self.owner[o.resource_id].add(u)
        for a in ds.activity:
            if a.event_type == "LINK_RESOURCE_IDENTITY":
                for u in re.findall(r"\buser_\d+\b", a.details):
                    self.owner[a.resource_id].add(u)
                for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", a.details):
                    for u in ids.by_key.get(("email", e.lower().rstrip(".")), ()):
                        self.owner[a.resource_id].add(u)
        self.owned_by: dict[str, set] = defaultdict(set)
        for rid, us in self.owner.items():
            for u in us:
                self.owned_by[u].add(rid)
        self.worker = [a for a in ds.activity if a.source_system == "deletion_worker"]

    # ------------------------------------------------------------------
    def _window(self, req: Request, lo: float, hi: float, accounts: bool) -> list[Activity]:
        rep = ts(req.reported_at)
        out = []
        for a in self.worker:
            is_acct = a.resource_id.startswith("account_") and a.event_type == "DELETE_RECORD"
            if is_acct != accounts:
                continue
            d = minutes(ts(a.event_time) - rep)
            if lo <= d <= hi:
                out.append(a)
            elif accounts and "Target resource" in a.details and abs(d) <= 20:
                # older workflow version logs the primary deletion right at reported_at
                out.append(a)
        return out

    def link_all(self) -> dict[str, CaseLinks]:
        reqs = self.ds.all_requests
        links: dict[str, CaseLinks] = {}
        for r in reqs:
            uid = self.ids.resolve_email(r.email)
            cl = CaseLinks(r, uid)
            if uid:
                cl.direct = set(self.owned_by.get(uid, set()))
            links[r.case_id] = cl
        self._split_repeat_subjects(links)

        # ---------------- primary account
        claims: dict[str, list] = defaultdict(list)   # account -> [(case, score)]
        for cid, cl in links.items():
            r = cl.request
            win = self._window(r, *ACCOUNT_WINDOW, accounts=True)
            own = [a for a in win if cl.user_id in self.owner.get(a.resource_id, ())]
            if own:
                cl.account = own[0].resource_id
                cl.activities.append(own[0])
                continue
            # direct account not in the generated window (hand-made cases): nearest deletion activity
            direct_accts = sorted(x for x in cl.direct if x.startswith("account_"))
            if direct_accts:
                acts = [a for a in self.worker if a.resource_id in direct_accts]
                if acts:
                    rep = ts(r.reported_at)
                    best = min(acts, key=lambda a: abs(minutes(ts(a.event_time) - rep)))
                    cl.account = best.resource_id
                    cl.activities.append(best)
                    continue
            free = [a for a in win if not self.owner.get(a.resource_id)]
            want = STATUS_RESULT.get(r.reported_status)
            for a in free:
                score = 0.0
                if a.reported_result == want or len(free) == 1:
                    score += 10
                myfiles = set().union(*(self.files_of[x] for x in cl.direct)) if cl.direct else set()
                if self.files_of.get(a.resource_id, set()) & myfiles:
                    score += 5
                score -= abs(minutes(ts(a.event_time) - ts(r.reported_at)) + 60) / 100.0
                claims[a.resource_id].append((cid, score, a))
        # greedy global assignment, best scores first
        cands = sorted(((s, rid, cid, a) for rid, lst in claims.items() for cid, s, a in lst),
                       key=lambda t: (-t[0], t[1], t[2]))
        for s, rid, cid, a in cands:
            cl = links[cid]
            if cl.account or s < 9:      # require matching reported result
                continue
            if any(l.account == rid for l in links.values()):
                continue
            cl.account = rid
            cl.activities.append(a)

        # ---------------- other workflow resources
        rclaims: dict[str, list] = defaultdict(list)
        for cid, cl in links.items():
            win = self._window(cl.request, *RESOURCE_WINDOW, accounts=False)
            for a in win:
                owners = self.owner.get(a.resource_id, set())
                if cl.user_id in owners:
                    cl.activities.append(a)
                    continue
                if owners:
                    continue
                rclaims[a.resource_id].append((cid, a))
        self._assign_unlinked(links, rclaims)
        return links

    # ------------------------------------------------------------------
    def _split_repeat_subjects(self, links: dict[str, CaseLinks]) -> None:
        """A person who filed several requests over time: give each directly-linked resource to
        the request whose [requested_at - 7d, audit_at] span is closest to when the resource was
        observed or processed."""
        by_user: dict[str, list] = defaultdict(list)
        for cl in links.values():
            if cl.user_id:
                by_user[cl.user_id].append(cl)
        times: dict[str, list] = defaultdict(list)
        for o in self.obs:
            times[o.resource_id].append(ts(o.ts))
        for a in self.ds.activity:      # activity claims only when nothing was observed
            if a.resource_id not in times or a.event_type == "LINK_RESOURCE_IDENTITY":
                times[a.resource_id].append(ts(a.event_time))
        for uid, cls in by_user.items():
            if len(cls) < 2:
                continue
            for rid in set().union(*(c.direct for c in cls)):
                def dist(cl: CaseLinks) -> float:
                    lo = ts(cl.request.requested_at).timestamp() - 7 * 86400
                    hi = ts(cl.request.audit_at).timestamp()
                    return min((0.0 if lo <= t.timestamp() <= hi else min(abs(t.timestamp() - lo), abs(t.timestamp() - hi)))
                               for t in times.get(rid, [])) if times.get(rid) else 0.0
                best = min(cls, key=lambda c: (dist(c), c.request.case_id))
                for c in cls:
                    if c is not best:
                        c.direct.discard(rid)

    @staticmethod
    def _kind(a: Activity) -> str:
        if "Policy action completed" in a.details or "reported the requested operation as completed" in a.details:
            return "policy"            # workflow applied the treatment (claims success)
        return "followup"              # final-result / status checks (typically still failing)

    def _bundle_order(self) -> tuple[dict[str, tuple[int, int]], set]:
        """Snapshot bundles list the records of a collection in subject order.

        Returns
          bounds - for a record with no identifier, the owners of its nearest identified
                   neighbours bound its owner's user number;
          before - pairs (r1, r2) of unidentified records where r1 is listed before r2, so the
                   owner of r1 should precede the owner of r2.
        """
        groups: dict[tuple, list] = defaultdict(list)
        for o in self.obs:
            if o.attrs.get("ctx_collection") and o.attrs.get("ctx_path") == "snapshots/records":
                groups[(o.file_id, o.attrs["ctx_captured_at"], o.attrs["ctx_collection"])].append(o)
        bounds: dict[str, tuple[int, int]] = {}
        before: set = set()
        for g in groups.values():
            nums = []
            for o in g:
                us = self.owner.get(o.resource_id, set())
                nums.append(min(int(u[5:]) for u in us) if len(us) == 1 else None)
            known = [n for n in nums if n is not None]
            if known != sorted(known):
                continue
            unknown = [o.resource_id for o, n in zip(g, nums) if n is None]
            for i, r1 in enumerate(unknown):
                for r2 in unknown[i + 1:]:
                    before.add((r1, r2))
            if not known:
                continue
            for i, o in enumerate(g):
                if nums[i] is not None:
                    continue
                lo = max([n for n in nums[:i] if n is not None], default=0)
                hi = min([n for n in nums[i + 1:] if n is not None], default=10**9)
                old = bounds.get(o.resource_id, (0, 10**9))
                bounds[o.resource_id] = (max(lo, old[0]), min(hi, old[1]))
        return bounds, before

    def _assign_unlinked(self, links: dict[str, CaseLinks], rclaims: dict[str, list]) -> None:
        bounds, self._before = self._bundle_order()
        # family of each case from the activities on its directly-linked resources
        fam: dict[str, set] = {cid: {self._kind(a) for a in cl.activities if not a.resource_id.startswith("account_")}
                               for cid, cl in links.items()}

        def weight(rid: str, cid: str, a: Activity) -> float:
            cl = links[cid]
            w = 1.0
            kind = self._kind(a)
            other = "followup" if kind == "policy" else "policy"
            if kind in fam[cid]:
                w += 2.0
            elif other in fam[cid]:
                w -= 2.0
            # the workflow applies treatments before it reports; final-result checks come after
            d = minutes(ts(a.event_time) - ts(cl.request.reported_at))
            if kind == "policy" and d > 5:
                w -= 2.5
            if kind == "followup" and d < -15:
                w -= 2.5
            pre = resource_prefix(rid)
            if any(resource_prefix(x) == pre for x in cl.direct):
                w -= 3.0           # already has its own record of this type
            b = bounds.get(rid)
            if b and cl.user_id:
                n = int(cl.user_id[5:])
                w += 2.0 if b[0] <= n <= b[1] else -2.0
            myfiles = set().union(*(self.files_of[x] for x in cl.resources)) if cl.resources else set()
            if self.files_of.get(rid, set()) & myfiles:
                w += 0.5
            w -= abs(minutes(ts(a.event_time) - ts(cl.request.reported_at))) / 1000.0
            return w

        # connected components of the claim graph -> exact assignment for small components
        res_of_case: dict[str, set] = defaultdict(set)
        for rid, lst in rclaims.items():
            for cid, _ in lst:
                res_of_case[cid].add(rid)
        seen: set = set()
        for start in sorted(rclaims):
            if start in seen:
                continue
            comp_r, comp_c, stack = set(), set(), [start]
            while stack:
                r = stack.pop()
                if r in comp_r:
                    continue
                comp_r.add(r)
                for cid, _ in rclaims[r]:
                    if cid not in comp_c:
                        comp_c.add(cid)
                        stack.extend(res_of_case[cid] - comp_r)
            seen |= comp_r
            self._solve_component(links, rclaims, sorted(comp_r), weight)

    def _solve_component(self, links, rclaims, rids, weight) -> None:
        # each resource -> one case; a case takes at most one resource per prefix
        options = {rid: [(cid, a, weight(rid, cid, a)) for cid, a in rclaims[rid]] for rid in rids}
        best = {"score": float("-inf"), "assign": {}}

        def order_bonus(assign) -> float:
            b = 0.0
            for r1, (c1, _) in assign.items():
                for r2, (c2, _) in assign.items():
                    if (r1, r2) in self._before:
                        u1, u2 = links[c1].user_id, links[c2].user_id
                        if u1 and u2 and u1 != u2:
                            b += 1.0 if int(u1[5:]) < int(u2[5:]) else -1.0
            return b

        if len(rids) <= 8 and all(len(o) <= 6 for o in options.values()):
            def rec(i, used, score, assign):
                if i == len(rids):
                    score += order_bonus(assign)
                    if score > best["score"]:
                        best["score"], best["assign"] = score, dict(assign)
                    return
                rid = rids[i]
                pre = resource_prefix(rid)
                for cid, a, w in sorted(options[rid], key=lambda t: -t[2]):
                    if (cid, pre) in used:
                        continue
                    assign[rid] = (cid, a)
                    rec(i + 1, used | {(cid, pre)}, score + w, assign)
                    del assign[rid]
                # leave it unassigned (scored like an implausible owner) so that components with
                # more same-prefix records than claimants still get a partial assignment
                rec(i + 1, used, score - 1.0, assign)
            rec(0, frozenset(), 0.0, {})
            chosen = best["assign"]
        else:   # greedy fallback for large components
            chosen, used = {}, set()
            flat = sorted(((w, rid, cid, a) for rid, o in options.items() for cid, a, w in o), key=lambda t: -t[0])
            for w, rid, cid, a in flat:
                pre = resource_prefix(rid)
                if rid in chosen or (cid, pre) in used:
                    continue
                chosen[rid] = (cid, a)
                used.add((cid, pre))
        for rid, (cid, a) in chosen.items():
            if weight(rid, cid, a) <= -1.0:
                continue          # no plausible owner: leave unassigned rather than guess
            links[cid].workflow.add(rid)
            links[cid].activities.append(a)
