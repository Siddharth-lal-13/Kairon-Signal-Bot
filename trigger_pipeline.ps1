# PowerShell script to trigger Kairon pipeline
$secret = "86b5c3b41ba96e10ee5226cc209bf82c"
$headers = @{
    "Content-Type" = "application/json"
    "X-N8n-Secret" = $secret
}
$body = @{
    run_id = "test-manual-bypass"
    triggered_at = (Get-Date).ToUniversalTime().ToString("s")
}

$response = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/trigger" -Headers $headers -Body (ConvertTo-Json $body)

Write-Host "Pipeline trigger response: $($response.StatusCode)"
Write-Host "Response body: $($response.Content)"
