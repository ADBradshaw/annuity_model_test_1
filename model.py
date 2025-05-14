from cashflower import variable, discount
from input import policy, assumption

from settings import settings

projection_length = settings["T_MAX_CALCULATION"]

# --- Policy variables

@variable()
def age_at_entry():
    return policy.get("age_at_entry")

@variable()
def gender():
    return policy.get("gender")

@variable()
def initial_guarantee_term():
    return policy.get("initial_guarantee_term")

@variable()
def initial_annuity():
    return policy.get("initial_annuity")

@variable()
def escalation_type():
    return policy.get("escalation_type")

@variable()
def escalation_rate():
    return policy.get("escalation_rate")

@variable()
def annuity_payment_frequency():
    return policy.get("annuity_payment_frequency")

@variable()
def duration_IF():
    return policy.get("duration_IF")

@variable()
def max_mortality_age():
    return assumption["max_mortality_age"]

@variable()
def mortality_overlay_factor():
    return float(assumption["mortality_overlay"].get_value("annuity", ("male" if gender() == 0 else "female")))

@variable()
def base_renewal_expense():
    return float(assumption["expense"].get_value("annuity", "renewal_expense"))

@variable()
def renewal_expense_inflation_margin():
    return float(assumption["expense"].get_value("annuity", "inflation_margin"))

@variable()
def initial_annuity_per_payment_period():
    return initial_annuity() / annuity_payment_frequency()

# --- Mortality & Survival ---

@variable()
def current_age(t):
    """Current age of the policyholder at time t (rounded down to nearest integer for q_x lookup)."""
    if t<12:
        return int(age_at_entry() + (duration_IF() + t) / 12)
    return current_age(t-12) + 1

@variable()
def death_rate(t):
    """Monthly mortality rate adjusted by experience factor, capped at 1.0 and set to 1.0 beyond max mortality age."""

    if t != 0 and current_age(t) == current_age(t-1):
        return death_rate(t-1)

    if current_age(t) <= max_mortality_age():

        q_x = float(assumption["mortality_base_table"].get_value(str(int(current_age(t))), ("male" if gender() == 0 else "female")))
        q_x_monthly = 1 - (1 - q_x) ** (1 / 12)

        return min(1.0, q_x_monthly * mortality_overlay_factor())

    return 1.0

# --- Policy Survival ---

@variable()
def expected_number_of_policies_IF(t):
    """Expected number of policies in force at time t."""
    if t == 0:
        return 1.0
    return expected_number_of_policies_IF(t - 1) * (1 - death_rate(t - 1))


# --- Guarantee Period Logic ---

@variable()
def within_guarantee_period(t):
    """Numeric flag (1/0) for whether t is within the guaranteed term since annuity commencement."""
    if t != 0 and within_guarantee_period(t-1) == 0:
        return 0
    duration_at_t = duration_IF() + t
    return int(duration_at_t < initial_guarantee_term())


@variable(array=True)
def within_guarantee_period_array():
    """List of numeric flags (1/0) for whether each projection period is within the guarantee term since commencement."""
    initial_guarantee_term = policy.get("initial_guarantee_term")
    duration_IF = policy.get("duration_IF")
    return [int((duration_IF + t) < initial_guarantee_term) for t in range(projection_length + 1)]


# --- Annuity Benefits ---

@variable()
def annuity_payment_per_policy(t):
    """Annuity payment per policy at time t, adjusted for escalation and frequency."""
    duration_at_t = duration_IF() + t
    payment_interval = 12 // annuity_payment_frequency()
    is_payment_month = (duration_at_t % payment_interval == 0)

    if not is_payment_month:
        return 0

    if escalation_type() == 0: # none
        return initial_annuity() / annuity_payment_frequency()

    if escalation_type() == 1: # fixed
        if t < 12:
            return initial_annuity_per_payment_period()
        previous_payment = annuity_payment_per_policy(t - 12)
        return previous_payment * (1 + escalation_rate())

    if escalation_type() == 2: # inflation
        if t < 12:
            return initial_annuity_per_payment_period()
        previous_payment = annuity_payment_per_policy(t - 12)
        inflation = float(assumption["inflation_forward"].get_value(str(t - 12), "rate"))
        return previous_payment * (1 + inflation)

    raise ValueError(f"Unsupported escalation_type: {escalation_type()}")


@variable()
def expected_annuity_payment(t):
    """Expected annuity payment at time t, accounting for guarantee and survival."""
    if within_guarantee_period(t):
        return annuity_payment_per_policy(t)
    return annuity_payment_per_policy(t) * expected_number_of_policies_IF(t)


# --- Renewal Expenses ---

@variable()
def renewal_expense_per_policy(t):
    """Annual renewal expense per policy, increasing with inflation and margin, paid only on policy anniversaries."""
    duration_at_t = duration_IF() + t

    if duration_at_t % 12 != 0:
        return 0

    if t < 12:
        return base_renewal_expense()

    previous_expense = renewal_expense_per_policy(t - 12)
    inflation = float(assumption["inflation_forward"].get_value(str(t - 12), "rate"))

    return previous_expense * (1 + inflation + renewal_expense_inflation_margin())


@variable()
def expected_renewal_expense_payment(t):
    """Expected renewal expense payment at time t, adjusted for survival."""
    return renewal_expense_per_policy(t) * expected_number_of_policies_IF(t)

# --- Investment Expenses ---

@variable()
def investment_expense_payment(t):
    """Investment expense at time t based on gross liability as proxy for invested assets."""
    investment_expense_rate = assumption["investment_expense"]
    return EPV_liability_gross(t) * investment_expense_rate


# --- Discounted Values ---

@variable(array=True)
def discount_rate():
    """Discount rate from one-year forward yield curve (used as discount factors)."""
    return [
        (1 + float(assumption["yield_curve_forward"].get_value(str(t), "rate"))) ** (1 / 12)
        for t in range(projection_length + 1)
    ]


@variable(array=True)
def EPV_annuity_benefit():
    """Present value of future expected annuity payments by projection month."""
    return discount(expected_annuity_payment(), discount_rate())


@variable(array=True)
def EPV_renewal_expenses():
    """Present value of future expected renewal expenses by projection month."""
    return discount(expected_renewal_expense_payment(), discount_rate())


@variable(array=True)
def EPV_investment_expenses():
    """Present value of future investment expenses by projection month."""
    return discount(investment_expense_payment(), discount_rate())


@variable(array=True)
def EPV_total_expenses():
    """Total present value of all expense components."""
    return EPV_renewal_expenses() + EPV_investment_expenses()


@variable(array=True)
def EPV_liability_gross():
    """Gross liability before investment expenses (for use as asset proxy)."""
    return EPV_annuity_benefit() + EPV_renewal_expenses()


@variable(array=True)
def EPV_liability():
    """Total liability: present value of all future outgo per projection period."""
    return EPV_annuity_benefit() + EPV_total_expenses()


@variable()
def liability():
    """Total liability at the valuation date (t=0)."""
    return EPV_liability(0)
