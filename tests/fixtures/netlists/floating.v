// a[1] and c are unused inputs, u is an undriven output, g2 drives a dangling net.
module top(a, b, c, y, z, u);
  input [1:0] a;
  input b, c;
  output y, z, u;
  wire w1, w2;
  AND2 g0(.Y(w1), .A(a[0]), .B(b));
  INV g1(.Y(y), .A(w1));
  BUF g2(.Y(w2), .A(b));
  INV g3(.Y(z), .A(a[0]));
endmodule
