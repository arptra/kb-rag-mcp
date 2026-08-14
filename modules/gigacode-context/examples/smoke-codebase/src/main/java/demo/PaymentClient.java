package demo;

import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

@Component
public class PaymentClient {
    private final WebClient webClient;

    public PaymentClient(WebClient.Builder builder) {
        this.webClient = builder.baseUrl("http://payment-service").build();
    }

    public void reserve(String customerId, long total) {
        webClient.post()
            .uri("/internal/payments/reserve")
            .bodyValue(new ReservePaymentRequest(customerId, total))
            .retrieve()
            .toBodilessEntity()
            .block();
    }
}
