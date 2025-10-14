## Introduction

Mollie API client generated with [progenitor](https://github.com/oxidecomputer/progenitor) using [Mollie openapi spec](https://github.com/mollie/openapi)

## Usage

### Create a Client
```rust
use mollie::Client;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION};

fn create_client() -> Result<Client, Box<dyn std::error::Error>> {
    let api_key = "your_api_key";
    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {}", api_key))?,
    );

    let http_client = reqwest::Client::builder()
        .default_headers(headers)
        .build()?;

    Ok(Client::new_with_client(
        "https://api.mollie.com/v2",
        http_client,
    ))
}
```

### Create a Payment
```rust
use mollie::types::{Amount, PaymentRequest};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = create_client()?;

    // Create a payment request
    let payment_request = PaymentRequest {
        amount: Some(Amount {
            currency: "EUR".to_string(),
            value: "10.00".to_string(),
        }),
        description: Some("Order #12345".parse()?),
        redirect_url: Some("https://example.com/return".to_string()),
        webhook_url: Some("https://example.com/webhook".to_string()),
        ..Default::default()
    };

    let response = client
        .create_payment(None, None, &payment_request)
        .await?;

    let payment = response.into_inner();
    println!("Payment created: {:?}", payment);

    Ok(())
}