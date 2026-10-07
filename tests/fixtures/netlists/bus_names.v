// Bus bit n42[0] next to a scalar port n420: a glob-style lookup of n42[0] could also match n420.
module top(n42, n420, n5, y);
  input [3:0] n42;
  input n420;
  output [1:0] n5;
  output y;
  wire n4;
  AND2 g0(.Y(n4), .A(n42[0]), .B(n420));
  XOR2 g1(.Y(n5[0]), .A(n4), .B(n42[1]));
  OR2 g2(.Y(n5[1]), .A(n42[2]), .B(n42[3]));
  INV g3(.Y(y), .A(n4));
endmodule
