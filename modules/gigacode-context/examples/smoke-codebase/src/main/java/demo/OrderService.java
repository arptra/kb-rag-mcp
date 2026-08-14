package demo;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

@Service
public class OrderService {
    private final OrderRepository orderRepository;
    private final PaymentClient paymentClient;

    public OrderService(OrderRepository orderRepository, PaymentClient paymentClient) {
        this.orderRepository = orderRepository;
        this.paymentClient = paymentClient;
    }

    @Transactional
    public Order createOrder(CreateOrderRequest request) {
        paymentClient.reserve(request.customerId(), request.total());
        Order order = Order.pending(request.customerId(), request.total());
        return orderRepository.save(order);
    }
}
