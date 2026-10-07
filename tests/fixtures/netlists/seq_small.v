// Known sequential depths: PI->D 2, reg->reg 3, reg->PO 1, PI->PO 2.
module top(clk, rst, a, b, y, z);
  input clk, rst, a, b;
  output y, z;
  wire q1, q2, w1, w2, w3, w4, w5, w6;
  INV g0(.Y(w1), .A(a));
  AND2 g1(.Y(w2), .A(w1), .B(b));
  DFF r1(.CLK(clk), .RN(rst), .SN(1'b1), .D(w2), .Q(q1));
  BUF g2(.Y(w3), .A(q1));
  INV g3(.Y(w4), .A(w3));
  OR2 g4(.Y(w5), .A(w4), .B(a));
  DFF r2(.CLK(clk), .RN(rst), .SN(1'b1), .D(w5), .Q(q2));
  INV g5(.Y(y), .A(q2));
  INV g6(.Y(w6), .A(b));
  XOR2 g7(.Y(z), .A(w6), .B(q1));
endmodule
