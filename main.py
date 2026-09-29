import os
import logging
import sys
from dotenv import load_dotenv  
import json
from typing import Optional
from fastmcp import FastMCP
from google.ads.googleads.client import GoogleAdsClient
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign
from facebook_business.adobjects.adsinsights import AdsInsights
from datetime import datetime, timedelta
from typing import Tuple, Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()  # Load environment variables from .env file

# 1. Fetch your secret API key from Railway environment variables
api_key = os.environ.get("MCP_API_KEY")

# Add your tools below as normal...
@mcp.tool
def my_tool():
    pass

#logging.basicConfig(level=logging.INFO)
# Ensure all logs go to stderr, NOT stdout
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True
)

# Initialize the server

mcp = FastMCP('MCP Analytics Server')

def get_date_range(days: int) -> Tuple[Optional[str], Optional[str]]:
    """Generates start_date and end_date in YYYY-MM-DD format.
    
    If days == 0, returns (None, None) representing 'ALL_TIME' / 'maximum'.
    """
    if days <= 0:
        return None, None
    
    today = datetime.now().date()
    # End date is usually yesterday for settled ad data
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=days - 1)
    
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")

#--------------------------------GOOGLE ADS------------------------------------------#
#Initialize Google Ads Client
def get_google_ads_client() -> GoogleAdsClient: # type: ignore
    """
    Initializes and returns a Google Ads client using the configuration from the environment variable.
    """
    
    credentials = {
        "client_id": os.getenv("GOOGLE_ADS_CLIENT_ID"),
        "client_secret": os.getenv("GOOGLE_ADS_CLIENT_SECRET"),
        "refresh_token": os.getenv("GOOGLE_ADS_REFRESH_TOKEN"),
        "use_proto_plus": True
    }
    return GoogleAdsClient.load_from_dict(credentials)

@mcp.tool()
def get_google_ads_status() -> str:
    """
    A tool that checks the status of the Google Ads API connection.
    Returns a string indicating whether the connection is successful or not.
    """
    from google.ads.googleads.client import GoogleAdsClient
    try:
        client = get_google_ads_client()
        customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID")
        ga_service = client.get_service("GoogleAdsService")
        
        query = "SELECT customer.id, customer.descriptive_name FROM customer LIMIT 1"
        response = ga_service.search(customer_id=customer_id, query=query)
        
        for row in response:
            return f"Connected to {row.customer.descriptive_name} (ID: {row.customer.id})"
            
        return f"Connected to Customer ID: {customer_id}"
    except Exception as e:
        return f"Google Ads Connection Error: {str(e)}"

@mcp.tool()
def fetch_google_ads_summary(client, customer_id: str, start_date: Optional[str], end_date: Optional[str]) -> dict:
    ga_service = client.get_service("GoogleAdsService")
    
    # Construct date condition
    if start_date and end_date:
        date_condition = f"WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'"
    else:
        date_condition = ""  # Omitting date filter pulls overall / all-time stats

    query = f"""
        SELECT 
            metrics.impressions, 
            metrics.clicks, 
            metrics.cost_micros,
            metrics.conversions_value
        FROM customer
        {date_condition}
    """
    
    response = ga_service.search(customer_id=customer_id, query=query)
    
    spend, clicks, impressions, conv_value = 0.0, 0, 0, 0.0
    for row in response:
        spend += row.metrics.cost_micros / 1000000.0
        clicks += row.metrics.clicks
        impressions += row.metrics.impressions
        conv_value += row.metrics.conversions_value

    roas = (conv_value / spend) if spend > 0 else 0.0

    return {
        "platform": "Google Ads",
        "impressions": impressions,
        "clicks": clicks,
        "spend_usd": round(spend, 2),
        "revenue_usd": round(conv_value, 2),
        "roas": round(roas, 2)
    }

@mcp.tool()
def get_google_ads_campaign_performance(customer_id: str) -> dict:
    """
    Fetches campaign performance data from Google Ads API for the given customer ID.
    Returns a dictionary containing campaign performance metrics.

    Args:
        customer_id: 10-digit Google Ads Customer ID (without dashes). 
                     Defaults to GOOGLE_ADS_CUSTOMER_ID env var if omitted.
    """
    try:
        target_id = customer_id or os.getenv("GOOGLE_ADS_CUSTOMER_ID")
        if not target_id:
            return "Customer ID must be provided either as an argument or in the environment variable."

        target_id = target_id.replace("-","").strip()  # Remove dashes if present
        client = get_google_ads_client()
        ga_service = client.get_service("GoogleAdsService")

        query = """
            SELECT
                campaign.id, 
                campaign.name, 
                campaign.status, 
                metrics.impressions, 
                metrics.clicks, 
                metrics.cost_micros,
                metrics.conversions_value
            FROM campaign
            WHERE campaign.status = 'ENABLED'
            LIMIT 10
        """

        response = ga_service.search(customer_id=target_id, query=query)

        results = []
        for row in response:
            cost = row.metrics.cost_micros / 1000000.0  # Convert micros to standard currency
            conv_value = row.metrics.conversions_value if row.metrics.conversions_value is not None else 0.0
            roas = (conv_value / cost) if cost > 0 else 0.0
            results.append({
                "campaign_id": row.campaign.id,
                "campaign_name": row.campaign.name,
                "status": row.campaign.status.name,
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "cost": round(cost, 2),
                "conversions_value": conv_value,
                "roas": round(roas, 2)
            })
        return {"results": results}

    except Exception as e:
        return f"Google ADS API Error: {str(e)}"

#--------------------------------META ADS------------------------------------------#
#Initialize Meta Ads Client
def init_meta_ads_client():
    """Initializes the global Meta Ads API session."""
    app_id = os.getenv("META_APP_ID")
    app_secret = os.getenv("META_APP_SECRET")
    access_token = os.getenv("META_ACCESS_TOKEN")
    
    FacebookAdsApi.init(app_id, app_secret, access_token)

@mcp.tool()
def get_meta_ads_status(ad_account_id: str = None) -> str:
    """Validates connectivity to the Meta Ads API and fetches account details.
    
    Args:
        ad_account_id: The Meta Ad Account ID (must start with 'act_'). 
                       Defaults to META_AD_ACCOUNT_ID env var if omitted.
    """
    try:
        init_meta_ads_client()
        target_id = ad_account_id or os.getenv("META_AD_ACCOUNT_ID")
        
        if not target_id:
            return "Error: No META_AD_ACCOUNT_ID provided."
            
        # Ensure 'act_' prefix exists
        if not target_id.startswith('act_'):
            target_id = f"act_{target_id}"

        # Fetch the ad account to verify access
        account = AdAccount(target_id)
        account.api_get(fields=[AdAccount.Field.name, AdAccount.Field.account_status])
        
        # Status 1 usually means ACTIVE
        status_map = {1: "ACTIVE", 2: "DISABLED", 3: "UNSETTLED", 7: "PENDING_RISK_REVIEW"}
        status_text = status_map.get(account[AdAccount.Field.account_status], "UNKNOWN")
        
        return f"Meta Ads API connection successful. Account: {account[AdAccount.Field.name]} (Status: {status_text})"

    except Exception as e:
        return f"Meta Ads API Error: {str(e)}"

@mcp.tool()
def fetch_meta_ads_summary(account, start_date: Optional[str], end_date: Optional[str]) -> dict:
    fields = [
        AdsInsights.Field.impressions,
        AdsInsights.Field.clicks,
        AdsInsights.Field.spend,
        AdsInsights.Field.action_values,
        AdsInsights.Field.purchase_roas,
    ]

    params = {"level": "account"}
    
    if start_date and end_date:
        params["time_range"] = {"since": start_date, "until": end_date}
    else:
        params["date_preset"] = "maximum"  # Overall / All-Time

    insights = account.get_insights(fields=fields, params=params)
    
    if not insights:
        return {"platform": "Meta Ads", "spend_usd": 0.0, "roas": 0.0, "clicks": 0, "impressions": 0}

    row = insights[0]
    spend = float(row.get("spend", 0.0))
    clicks = int(row.get("clicks", 0))
    impressions = int(row.get("impressions", 0))

    revenue = 0.0
    action_values = row.get("action_values", [])
    if action_values:
        for action in action_values:
            if action.get("action_type") in ["omni_purchase", "purchase"]:
                revenue += float(action.get("value", 0.0))

    roas = (revenue / spend) if spend > 0 else 0.0

    return {
        "platform": "Meta Ads",
        "impressions": impressions,
        "clicks": clicks,
        "spend_usd": round(spend, 2),
        "revenue_usd": round(revenue, 2),
        "roas": round(roas, 2)
    }

@mcp.tool()
def get_meta_campaign_report(
    date_preset: str = "last_30d",
    ad_account_id: Optional[str] = None
) -> str:
    """Fetches campaign-level performance metrics (Impressions, Clicks, CTR, Spend, CPC, ROAS) 
    from Meta Ads.

    Args:
        date_preset: Reporting time window. Options: 'today', 'yesterday', 'last_7d', 
                     'last_14d', 'last_30d', 'this_month', 'last_month', 'maximum'. 
                     Defaults to 'last_30d'.
        ad_account_id: Meta Ad Account ID (e.g., 'act_1234567890'). 
                       Defaults to META_AD_ACCOUNT_ID env var if omitted.
    """
    try:
        init_meta_ads_client()
        target_id = ad_account_id or os.getenv("META_AD_ACCOUNT_ID")

        if not target_id:
            return "Error: Missing META_AD_ACCOUNT_ID environment variable or parameter."

        if not target_id.startswith("act_"):
            target_id = f"act_{target_id}"

        account = AdAccount(target_id)

        # Meta Insights API requested fields
        fields = [
            AdsInsights.Field.campaign_id,
            AdsInsights.Field.campaign_name,
            AdsInsights.Field.impressions,
            AdsInsights.Field.clicks,
            AdsInsights.Field.ctr,
            AdsInsights.Field.spend,
            AdsInsights.Field.cpc,
            AdsInsights.Field.purchase_roas,
            AdsInsights.Field.action_values,
        ]

        params = {
            "level": "campaign",
            "date_preset": date_preset,
        }

        insights = account.get_insights(fields=fields, params=params)

        report = []
        for row in insights:
            spend = float(row.get("spend", 0.0))
            impressions = int(row.get("impressions", 0))
            clicks = int(row.get("clicks", 0))
            ctr = float(row.get("ctr", 0.0))
            cpc = float(row.get("cpc", 0.0))

            # Extract ROAS (Return on Ad Spend)
            roas = 0.0
            roas_data = row.get("purchase_roas")
            if roas_data and isinstance(roas_data, list):
                # Usually present as [{'action_type': 'omni_purchase', 'value': '2.45'}]
                roas = float(roas_data[0].get("value", 0.0))

            # Extract total conversion value if ROAS field wasn't populated directly
            conversion_value = 0.0
            action_values = row.get("action_values", [])
            if action_values:
                for action in action_values:
                    if action.get("action_type") in ["omni_purchase", "purchase"]:
                        conversion_value += float(action.get("value", 0.0))
            
            if roas == 0.0 and spend > 0 and conversion_value > 0:
                roas = round(conversion_value / spend, 2)

            report.append({
                "campaign_id": row.get("campaign_id"),
                "campaign_name": row.get("campaign_name"),
                "impressions": impressions,
                "clicks": clicks,
                "ctr_percent": round(ctr, 2),
                "spend_usd": round(spend, 2),
                "cpc_usd": round(cpc, 2),
                "purchase_value_usd": round(conversion_value, 2),
                "roas": round(roas, 2),
            })

        if not report:
            return f"No campaign activity found for the selected timeframe ('{date_preset}')."

        return json.dumps({"date_preset": date_preset, "campaigns": report}, indent=2)

    except Exception as e:
        return f"Meta Ads Reporting Error: {str(e)}"


#--------------------------------COMPARISON------------------------------------------#

@mcp.tool()
def compare_ad_performance(days: int = 30) -> str:
    """Compares Google Ads vs Meta Ads metrics (Spend, Revenue, Clicks, ROAS) side-by-side.

    Args:
        days: Number of past days to analyze (e.g., 7, 30, 60, 90). 
              Pass 0 for 'overall' / 'all-time' performance.
    """
    try:
        start_date, end_date = get_date_range(days)
        timeframe_label = f"Last {days} days ({start_date} to {end_date})" if days > 0 else "Overall / All-Time"

        # Fetch Google Ads Data
        google_client = get_google_ads_client()
        google_customer_id = os.getenv("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
        google_data = fetch_google_ads_summary(google_client, google_customer_id, start_date, end_date)

        # Fetch Meta Ads Data
        init_meta_ads_client()
        meta_account_id = os.getenv("META_AD_ACCOUNT_ID")
        if not meta_account_id.startswith("act_"):
            meta_account_id = f"act_{meta_account_id}"
        meta_account = AdAccount(meta_account_id)
        meta_data = fetch_meta_ads_summary(meta_account, start_date, end_date)

        # Calculate Combined Totals
        total_spend = google_data["spend_usd"] + meta_data["spend_usd"]
        total_revenue = google_data["revenue_usd"] + meta_data["revenue_usd"]
        overall_roas = round(total_revenue / total_spend, 2) if total_spend > 0 else 0.0

        comparison_report = {
            "timeframe": timeframe_label,
            "google_ads": google_data,
            "meta_ads": meta_data,
            "blended_totals": {
                "total_spend_usd": round(total_spend, 2),
                "total_revenue_usd": round(total_revenue, 2),
                "blended_roas": overall_roas
            }
        }

        return json.dumps(comparison_report, indent=2)

    except Exception as e:
        return f"Comparison Error: {str(e)}"
    
#--------------------------------RUN THE SERVER------------------------------------------#
# 2. Define the Authentication Middleware
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Retrieve the expected API key from Railway environment variables
        expected_key = os.environ.get("MCP_API_KEY")
        
        # If an API key is configured on the server, enforce verification
        if expected_key:
            auth_header = request.headers.get("Authorization")
            expected_header_value = f"Bearer {expected_key}"
            
            if not auth_header or auth_header != expected_header_value:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Unauthorized: Invalid or missing Bearer token"}
                )
                
        # Proceed with the request if auth passes (or if no key is set)
        response = await call_next(request)
        return response

# 3. Attach the middleware to FastMCP's underlying Starlette app
mcp.app.add_middleware(BearerAuthMiddleware)
if __name__ == "__main__":
    # Binds the server to 0.0.0.0 so Docker can expose it, and forces SSE transport
    #mcp.run(transport="sse", host="0.0.0.0", port=80)
    #mcp.run()  # Defaults to stdio transport

    # Railway passes the assigned port as a string. Fall back to 8080 for local testing.
    port = int(os.environ.get("PORT", "8080"))
    
    # Host MUST be 0.0.0.0 to accept external traffic in the cloud
    mcp.run(transport="http", host="0.0.0.0", port=port, path="/mcp")
