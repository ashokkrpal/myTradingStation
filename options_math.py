import math
import scipy.stats as stat

def calculate_d1_d2(S, K, t, r, sigma):
    """Calculates d1 and d2 for Black-Scholes."""
    if t <= 0.0 or sigma <= 0.0: return 0.0, 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    return d1, d2

def bs_price(S, K, t, r, sigma, opt_type):
    """Calculates theoretical option price."""
    if t <= 0.0: return max(0.0, S - K) if opt_type == 'CE' else max(0.0, K - S)
    d1, d2 = calculate_d1_d2(S, K, t, r, sigma)
    if opt_type == 'CE':
        return S * stat.norm.cdf(d1) - K * math.exp(-r * t) * stat.norm.cdf(d2)
    else:
        return K * math.exp(-r * t) * stat.norm.cdf(-d2) - S * stat.norm.cdf(-d1)

def calculate_iv(LTP, S, K, t, r, opt_type, initial_guess=0.2):
    """Reverse-engineers Implied Volatility from live LTP using Newton-Raphson."""
    if t <= 0.0 or LTP <= 0.0: return 0.0
    sigma = initial_guess
    for _ in range(100):
        price = bs_price(S, K, t, r, sigma, opt_type)
        diff = price - LTP
        if abs(diff) < 0.01: break
        d1, _ = calculate_d1_d2(S, K, t, r, sigma)
        vega = S * stat.norm.pdf(d1) * math.sqrt(t)
        if vega == 0.0: break
        sigma -= diff / vega
        if sigma <= 0.0: sigma = 0.001 
    return sigma

def calculate_greeks(S, K, t, r, sigma, opt_type):
    """Calculates Delta and Theta."""
    if t <= 0.0: return 0.0, 0.0
    d1, d2 = calculate_d1_d2(S, K, t, r, sigma)
    if opt_type == 'CE':
        delta = stat.norm.cdf(d1)
        theta = (- (S * stat.norm.pdf(d1) * sigma) / (2 * math.sqrt(t)) - r * K * math.exp(-r * t) * stat.norm.cdf(d2)) / 365
    else:
        delta = stat.norm.cdf(d1) - 1
        theta = (- (S * stat.norm.pdf(d1) * sigma) / (2 * math.sqrt(t)) + r * K * math.exp(-r * t) * stat.norm.cdf(-d2)) / 365
    return delta, theta