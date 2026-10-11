import Std

/- Hand-authored accounting abstraction: two pools, one currency, arbitrary
   recipient identifiers. Fee inputs and transaction outcomes come from replay.
   No AMM math, EVM arithmetic, calldata execution, token implementation,
   EVM correspondence or creator approval is asserted. -/
namespace Rebate

abbrev Recipient := Nat

structure Account where
  collected : Nat := 0
  paid : Nat := 0
  earned : Recipient → Nat := fun _ => 0
  claimed : Recipient → Nat := fun _ => 0

structure State where
  a : Account := {}
  b : Account := {}
  custody : Nat := 0

inductive Pool where | a | b deriving DecidableEq, Repr
inductive Action where
  | collect (pool : Pool) (recipient : Recipient) (fee : Nat)
  | claim (pool : Pool) (caller : Recipient) (amount : Nat)
  | noAccountingEffect
  | rejected
  | transferFailure (pool : Pool) (caller : Recipient) (amount : Nat)
  deriving Repr

def get (s : State) : Pool → Account | .a => s.a | .b => s.b

def put (s : State) (p : Pool) (x : Account) (balance : Nat) : State :=
  match p with
  | .a => { s with a := x, custody := balance }
  | .b => { s with b := x, custody := balance }

def credit (x : Account) (u : Recipient) (fee : Nat) : Account :=
  { x with
    collected := x.collected + fee
    earned := fun v => if v = u then x.earned v + fee / 2 else x.earned v }

def pay (x : Account) (u : Recipient) (n : Nat) : Account :=
  { x with
    paid := x.paid + n
    claimed := fun v => if v = u then x.claimed v + n else x.claimed v }

-- Collection represents authenticated, successful fee collection. Claim
-- represents an authenticated caller and a successful transfer if admitted.
-- Failed transactions have no committed effects. noAccountingEffect is only
-- a projection onto the hook ledger, not onto external balances or allowances.
def step (fixed : Bool) (s : State) : Action → State
  | .collect p u fee => put s p (credit (get s p) u fee) (s.custody + fee)
  | .claim p u n =>
    let x := get s p
    let permitted := if fixed then
        x.claimed u + n ≤ x.earned u ∧ x.paid + n ≤ x.collected
      else n ≤ x.earned u
    if 0 < n ∧ n ≤ s.custody ∧ permitted then
      put s p (pay x u n) (s.custody - n)
    else s
  | .noAccountingEffect => s
  | .rejected => s
  | .transferFailure _ _ _ => s

def SafeAccount (x : Account) : Prop :=
  x.paid ≤ x.collected ∧ ∀ u, x.claimed u ≤ x.earned u

def Safe (s : State) : Prop := SafeAccount s.a ∧ SafeAccount s.b ∧
  s.custody + s.a.paid + s.b.paid = s.a.collected + s.b.collected

theorem credit_safe (x : Account) (u : Recipient) (fee : Nat)
    (h : SafeAccount x) : SafeAccount (credit x u fee) := by
  refine ⟨?_, ?_⟩
  · change x.paid ≤ x.collected + fee
    have hp := h.1
    omega
  · intro v
    by_cases hv : v = u
    · subst v
      have hu := h.2 u
      have hcredit : x.claimed u ≤ x.earned u + fee / 2 := by omega
      simpa [credit] using hcredit
    · simpa [credit, hv] using h.2 v

theorem pay_safe (x : Account) (u : Recipient) (n : Nat)
    (h : SafeAccount x) (hu : x.claimed u + n ≤ x.earned u)
    (hp : x.paid + n ≤ x.collected) : SafeAccount (pay x u n) := by
  refine ⟨hp, ?_⟩
  intro v
  by_cases hv : v = u
  · subst v
    simpa [pay] using hu
  · simpa [pay, hv] using h.2 v

theorem step_safe (s : State) (a : Action) (h : Safe s) : Safe (step true s a) := by
  rcases h with ⟨ha, hb, hc⟩
  cases a with
  | collect p u f =>
    cases p <;> simp only [step, get, put, Safe]
    · exact ⟨credit_safe s.a u f ha, hb, by simp only [credit]; omega⟩
    · exact ⟨ha, credit_safe s.b u f hb, by simp only [credit]; omega⟩
  | claim p u n =>
    cases p <;> simp only [step, get, ↓reduceIte]
    · split
      next hn =>
        simp only [put, Safe]
        exact ⟨pay_safe s.a u n ha hn.2.2.1 hn.2.2.2,
          hb, by simp only [pay]; omega⟩
      next => exact ⟨ha, hb, hc⟩
    · split
      next hn =>
        simp only [put, Safe]
        exact ⟨ha, pay_safe s.b u n hb hn.2.2.1 hn.2.2.2,
          by simp only [pay]; omega⟩
      next => exact ⟨ha, hb, hc⟩
  | noAccountingEffect => exact ⟨ha, hb, hc⟩
  | rejected => exact ⟨ha, hb, hc⟩
  | transferFailure p u n => exact ⟨ha, hb, hc⟩

def run (fixed : Bool) (xs : List Action) : State := xs.foldl (step fixed) {}

theorem run_safe_from (xs : List Action) (s : State) (h : Safe s) :
    Safe (xs.foldl (step true) s) := by
  induction xs generalizing s with
  | nil => exact h
  | cons a xs ih => exact ih (step true s a) (step_safe s a h)

theorem repaired_all_traces_safe (xs : List Action) : Safe (run true xs) :=
  run_safe_from xs {} (by simp [Safe, SafeAccount])

theorem other_pool_unchanged (fixed : Bool) (s : State) (p q : Pool)
    (u : Recipient) (n : Nat) (h : p ≠ q) :
    get (step fixed s (.claim p u n)) q = get s q := by
  cases p <;> cases q <;> simp_all [step, get, put]
  all_goals split <;> split <;> rfl

theorem pay_other_recipient_unchanged (x : Account) (u v : Recipient)
    (n : Nat) (h : v ≠ u) :
    (pay x u n).claimed v = x.claimed v ∧
    (pay x u n).earned v = x.earned v := by
  simp [pay, h]

theorem rejected_unchanged (fixed : Bool) (s : State) :
    step fixed s .rejected = s := rfl

theorem failed_transfer_unchanged (fixed : Bool) (s : State)
    (p : Pool) (u : Recipient) (n : Nat) :
    step fixed s (.transferFailure p u n) = s := rfl

-- Historical single-user fee/claim trace, with recipient 0.
def attack : List Action := [.collect .a 0 99, .collect .b 0 99,
  .claim .a 0 49, .claim .a 0 49, .claim .a 0 49]

theorem seeded_attack : (run false attack).a.paid = 147 ∧
    (run false attack).custody = 51 ∧ (run false attack).a.collected = 99 := by decide

theorem repaired_attack : (run true attack).a.paid = 49 ∧
    (run true attack).custody = 149 := by decide

theorem repaired_preserves_other_reserve (xs : List Action) :
    (run true xs).b.collected - (run true xs).b.paid ≤ (run true xs).custody := by
  have h := repaired_all_traces_safe xs
  simp only [Safe, SafeAccount] at h
  omega

-- Hook-ledger projection of tx-1 through tx-6. Recipient 0 is self and 1
-- is Bob. Funding and approval succeed but do not affect this ledger.
-- Successful raw router/claim calls are decoded accounting actions.
def multiRecipientTrace : List Action :=
  [.collect .a 0 99, .noAccountingEffect, .noAccountingEffect,
   .collect .a 1 199, .claim .a 1 99, .claim .a 0 49]

theorem multi_recipient_replay :
    (run true multiRecipientTrace).a.collected = 298 ∧
    (run true multiRecipientTrace).a.earned 0 = 49 ∧
    (run true multiRecipientTrace).a.earned 1 = 99 ∧
    (run true multiRecipientTrace).a.claimed 0 = 49 ∧
    (run true multiRecipientTrace).a.claimed 1 = 99 ∧
    (run true multiRecipientTrace).a.paid = 148 ∧
    (run true multiRecipientTrace).custody = 150 := by decide

#print axioms repaired_all_traces_safe
#print axioms other_pool_unchanged
#print axioms seeded_attack
#print axioms repaired_attack
#print axioms repaired_preserves_other_reserve
#print axioms multi_recipient_replay
end Rebate
